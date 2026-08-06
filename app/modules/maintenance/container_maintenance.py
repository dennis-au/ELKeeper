"""Assignment-scoped planned container maintenance.

The service coordinates one already-previewed managed workload. Production
assembly intentionally registers no runtime adapter: stop/start remains behind
the release capability gate while this module provides a fully testable,
checkpointed orchestration contract.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Callable, Protocol

from app.modules.clusters import ClusterRepository
from app.modules.workloads import WorkloadRepository

from .execution import (
    AdapterResult,
    MaintenanceAction,
    MaintenanceActionTicket,
    MaintenanceAdapterRequest,
    MaintenanceExecutionService,
)
from .executor import validate_managed_unit
from .lifecycle import MaintenanceState, MaintenanceStepState, SideEffectState
from .planned_contracts import MaintenanceWorkflowState
from .runtime import ControllerMaintenanceIO
from .store import MaintenanceRepository, PlanRecord, RecordNotFound


class ContainerMaintenanceError(RuntimeError):
    """A container maintenance operation could not reach a verified boundary."""


@dataclass(frozen=True)
class ManagedContainerTarget:
    """The exact controller-managed systemd unit selected for maintenance."""

    assignment_id: int
    cluster_id: int
    node_id: int
    role: str
    unit: str
    data_bearing: bool

    def __post_init__(self) -> None:
        if self.assignment_id < 1 or self.cluster_id < 1 or self.node_id < 1:
            raise ValueError("Managed container identity must use positive identifiers")
        if not self.role.strip():
            raise ValueError("Managed container role must not be blank")
        validate_managed_unit(self.unit)


@dataclass(frozen=True)
class RuntimeActionResult:
    """Redacted confirmation from a runtime or companion adapter."""

    confirmed: bool
    detail: str = ""


class ContainerRuntime(Protocol):
    async def stop(self, target: ManagedContainerTarget) -> RuntimeActionResult: ...

    async def start(self, target: ManagedContainerTarget) -> RuntimeActionResult: ...

    async def ready(self, target: ManagedContainerTarget) -> RuntimeActionResult: ...


class ControllerManagedWorkloadRuntime:
    """Restrict container lifecycle actions to one controller-managed unit."""

    def __init__(self, io: ControllerMaintenanceIO) -> None:
        self.io = io

    async def stop(self, target: ManagedContainerTarget) -> RuntimeActionResult:
        confirmed = await self.io.stop_managed_unit(node_id=target.node_id, unit=target.unit)
        return RuntimeActionResult(confirmed=confirmed, detail="managed-unit-stop")

    async def start(self, target: ManagedContainerTarget) -> RuntimeActionResult:
        confirmed = await self.io.start_managed_unit(node_id=target.node_id, unit=target.unit)
        return RuntimeActionResult(confirmed=confirmed, detail="managed-unit-start")

    async def ready(self, target: ManagedContainerTarget) -> RuntimeActionResult:
        states = await self.io.unit_states(node_id=target.node_id, units=(target.unit,))
        return RuntimeActionResult(
            confirmed=states.get(target.unit) is True,
            detail="managed-unit-active",
        )


class AssignmentCompanionReconciler(Protocol):
    async def reconcile(self, *, assignment_id: int, run_id: int) -> RuntimeActionResult: ...


class AllocationGuard(Protocol):
    async def capture(self, *, plan_id: str, cluster_id: int): ...

    async def activate(self, *, plan_id: str, cluster_id: int): ...

    async def restore(self, *, plan_id: str, cluster_id: int): ...


TargetResolver = Callable[[int], ManagedContainerTarget]


def resolve_managed_container_target(connection, assignment_id: int) -> ManagedContainerTarget:
    """Resolve only the selected active workload through public owner contracts."""

    assignment = WorkloadRepository.from_connection(connection).record_in_connection(connection, assignment_id)
    if assignment is None or assignment.get("state") != "active":
        raise ContainerMaintenanceError("Selected workload is not an active managed assignment")
    cluster = ClusterRepository.from_connection(connection).record_in_connection(
        connection, int(assignment["cluster_id"]),
    )
    if cluster is None or not isinstance(cluster.get("slug"), str) or not cluster["slug"]:
        raise ContainerMaintenanceError("Selected workload cluster identity is incomplete")
    role = str(assignment.get("role") or "")
    node_id = int(assignment["node_id"])
    unit = f"ecp-{cluster['slug']}-{role}-{node_id}.service"
    return ManagedContainerTarget(
        assignment_id=int(assignment["id"]),
        cluster_id=int(assignment["cluster_id"]),
        node_id=node_id,
        role=role,
        unit=unit,
        data_bearing=role in {"hot", "warm", "cold", "frozen", "content"} or role.startswith("data_"),
    )


class ContainerMaintenanceService:
    """Prepare, stop, and return exactly one selected managed unit.

    Plan, lock, run, and assignment-state ownership stay durable. The runtime
    port is injected so this service cannot accidentally acquire an SSH or
    Podman mutation path merely by being imported or registered in FastAPI.
    """

    _STEP_SEQUENCE = {
        "prepare": 100,
        "stop": 200,
        "return": 300,
        "verify": 400,
        "companions": 500,
    }

    def __init__(
        self,
        repository: MaintenanceRepository,
        *,
        execution: MaintenanceExecutionService,
        target_resolver: TargetResolver | None = None,
        runtime: ContainerRuntime,
        companions: AssignmentCompanionReconciler,
        allocation_guard: AllocationGuard | None = None,
        progress: Callable[[int, str], None] | None = None,
    ) -> None:
        self.repository = repository
        self.execution = execution
        self.target_resolver = target_resolver or (
            lambda assignment_id: resolve_managed_container_target(self.repository.connection, assignment_id)
        )
        self.runtime = runtime
        self.companions = companions
        self.allocation_guard = allocation_guard
        self.progress = progress

    async def prepare(self, plan_id: str, *, username: str):
        plan, target = self._plan_and_target(plan_id)
        if target.data_bearing and self.allocation_guard is None:
            raise ContainerMaintenanceError("Data-bearing container maintenance requires an allocation guard")
        ticket = self.execution.prepare(plan.id, MaintenanceAction.EXECUTE, username=username)
        try:
            self._progress(ticket.run_id, "Preparing selected managed workload.\n")
            state = self._transition_assignment(plan, MaintenanceWorkflowState.PREPARING)
            self._claim_target(plan, target, ticket.run_id)
            self._record_verified_step(plan, target, "prepare", {"run_id": ticket.run_id})
            if target.data_bearing:
                assert self.allocation_guard is not None
                self._progress(ticket.run_id, "Capturing and activating the data-node allocation guard.\n")
                self._durable_boundary()
                await self.allocation_guard.capture(plan_id=plan.id, cluster_id=target.cluster_id)
                self._durable_boundary()
                await self.allocation_guard.activate(plan_id=plan.id, cluster_id=target.cluster_id)
            state = self.repository.transition_assignment_state(
                target.assignment_id,
                state.state_revision,
                MaintenanceWorkflowState.READY_TO_STOP,
                plan.id,
            )
            self._audit(plan, username, "container-maintenance-ready-to-stop", target, {"run_id": ticket.run_id})
            self._progress(ticket.run_id, "Selected managed workload is ready to stop.\n")
            return state
        except Exception as error:
            self._recover(plan, target, ticket, username, "prepare", error)
            raise

    async def stop(self, plan_id: str, *, username: str):
        plan, target = self._plan_and_target(plan_id)
        state = self._require_assignment_state(plan, target, MaintenanceWorkflowState.READY_TO_STOP)
        ticket = self._ticket(plan, username)
        try:
            state = self.repository.transition_assignment_state(
                target.assignment_id,
                state.state_revision,
                MaintenanceWorkflowState.STOPPING,
                plan.id,
            )
            self._record_started_step(plan, target, "stop")
            self._progress(ticket.run_id, "Stopping selected managed workload.\n")
            self._durable_boundary()
            result = await self.runtime.stop(target)
            self._require_confirmation(result, "stop")
            self._record_verified_step(plan, target, "stop", {"unit": target.unit})
            state = self.repository.transition_assignment_state(
                target.assignment_id,
                state.state_revision,
                MaintenanceWorkflowState.MAINTENANCE,
                plan.id,
            )
            self._audit(plan, username, "container-maintenance-stopped", target, {})
            return state
        except Exception as error:
            self._recover(plan, target, ticket, username, "stop", error)
            raise

    async def return_to_service(self, plan_id: str, *, username: str):
        plan, target = self._plan_and_target(plan_id)
        state = self._require_assignment_state(plan, target, MaintenanceWorkflowState.MAINTENANCE)
        ticket = self._ticket(plan, username)
        try:
            state = self.repository.transition_assignment_state(
                target.assignment_id,
                state.state_revision,
                MaintenanceWorkflowState.RETURNING,
                plan.id,
            )
            self._record_started_step(plan, target, "return")
            self._progress(ticket.run_id, "Returning selected managed workload to service.\n")
            self._durable_boundary()
            self._require_confirmation(await self.runtime.start(target), "start")
            self._record_verified_step(plan, target, "return", {"unit": target.unit})
            state = self.repository.transition_assignment_state(
                target.assignment_id,
                state.state_revision,
                MaintenanceWorkflowState.VERIFYING,
                plan.id,
            )
            self._record_started_step(plan, target, "verify")
            self._progress(ticket.run_id, "Verifying selected managed workload readiness.\n")
            self._durable_boundary()
            self._require_confirmation(await self.runtime.ready(target), "readiness")
            self._record_verified_step(plan, target, "verify", {"unit": target.unit})
            self._record_started_step(plan, target, "companions")
            self._progress(ticket.run_id, "Scheduling selected workload companion reconciliation.\n")
            self._durable_boundary()
            self._require_confirmation(
                await self.companions.reconcile(assignment_id=target.assignment_id, run_id=ticket.run_id),
                "companion reconciliation",
            )
            self._record_verified_step(plan, target, "companions", {"assignment_id": target.assignment_id})
            if target.data_bearing:
                assert self.allocation_guard is not None
                self._durable_boundary()
                await self.allocation_guard.restore(plan_id=plan.id, cluster_id=target.cluster_id)
            if not WorkloadRepository.from_connection(self.repository.connection).release_assignment_operation_in_connection(
                self.repository.connection,
                assignment_id=target.assignment_id,
                run_id=ticket.run_id,
            ):
                raise ContainerMaintenanceError("Selected workload operation claim could not be released")
            state = self.repository.transition_assignment_state(
                target.assignment_id,
                state.state_revision,
                MaintenanceWorkflowState.AVAILABLE,
                None,
            )
            self.execution.finalize(ticket, AdapterResult(lifecycle_state=MaintenanceState.SUCCEEDED))
            self._audit(plan, username, "container-maintenance-returned", target, {"run_id": ticket.run_id})
            return state
        except Exception as error:
            self._recover(plan, target, ticket, username, "return", error)
            raise

    async def aclose(self) -> None:
        """Release action-scoped transport resources without changing state."""

        close = getattr(self.allocation_guard, "aclose", None)
        if close is not None:
            result = close()
            if inspect.isawaitable(result):
                await result

    def _plan_and_target(self, plan_id: str) -> tuple[PlanRecord, ManagedContainerTarget]:
        plan = self.repository.get_plan(plan_id)
        if plan.operation_kind != "workload_restart":
            raise ContainerMaintenanceError("Maintenance plan is not a container-maintenance plan")
        manifest = plan.target_manifest
        if manifest.get("public_operation") != "container_maintenance":
            raise ContainerMaintenanceError("Maintenance plan was not previewed for container maintenance")
        if plan.target_assignment_id is None:
            raise ContainerMaintenanceError("Maintenance plan has no selected workload assignment")
        target = self.target_resolver(plan.target_assignment_id)
        if (target.assignment_id, target.cluster_id, target.node_id) != (
            plan.target_assignment_id,
            plan.target_cluster_id,
            plan.target_node_id,
        ):
            raise ContainerMaintenanceError("Selected workload identity no longer matches the maintenance plan")
        return plan, target

    @staticmethod
    def _require_confirmation(result: RuntimeActionResult, action: str) -> None:
        if not result.confirmed:
            raise ContainerMaintenanceError(f"Container {action} was not confirmed")

    def _claim_target(self, plan: PlanRecord, target: ManagedContainerTarget, run_id: int) -> None:
        revisions = plan.target_manifest.get("assignment_revisions", ())
        expected = next(
            (
                item.get("revision")
                for item in revisions
                if isinstance(item, dict) and item.get("assignment_id") == target.assignment_id
            ),
            None,
        )
        if isinstance(expected, bool) or not isinstance(expected, int) or expected < 1:
            raise ContainerMaintenanceError("Maintenance plan has no valid selected workload revision")
        if not WorkloadRepository.from_connection(self.repository.connection).claim_assignment_operation_in_connection(
            self.repository.connection,
            assignment_id=target.assignment_id,
            expected_revision=expected,
            run_id=run_id,
        ):
            raise ContainerMaintenanceError("Selected workload changed or is already claimed by another operation")

    def _require_assignment_state(
        self,
        plan: PlanRecord,
        target: ManagedContainerTarget,
        expected: MaintenanceWorkflowState,
    ):
        state = self.repository.get_assignment_state(target.assignment_id)
        if state.workflow_state != expected or state.active_plan_id != plan.id:
            raise ContainerMaintenanceError(
                f"Container maintenance action is not available while the workload is {state.workflow_state.value}"
            )
        return state

    def _transition_assignment(self, plan: PlanRecord, target: MaintenanceWorkflowState):
        assignment_id = plan.target_assignment_id
        if assignment_id is None:
            raise ContainerMaintenanceError("Maintenance plan has no selected workload assignment")
        current = self.repository.get_assignment_state(assignment_id)
        return self.repository.transition_assignment_state(
            assignment_id,
            current.state_revision,
            target,
            plan.id,
        )

    def _ticket(self, plan: PlanRecord, username: str) -> MaintenanceActionTicket:
        if plan.run_id is None:
            raise ContainerMaintenanceError("Maintenance plan has no active run")
        locks = self.repository.list_active_locks(plan.id)
        if not locks:
            raise ContainerMaintenanceError("Maintenance plan no longer owns its locks")
        return MaintenanceActionTicket(
            request=MaintenanceAdapterRequest(
                plan_id=plan.id,
                run_id=plan.run_id,
                action=MaintenanceAction.EXECUTE,
                operation_kind=plan.operation_kind,
                target_node_id=plan.target_node_id,
                plan_hash=plan.plan_hash,
                requested_by=username,
            ),
            owner_token=locks[0].owner_token,
        )

    def _step(self, plan: PlanRecord, target: ManagedContainerTarget, name: str):
        return self.repository.create_step(
            plan_id=plan.id,
            step_key=f"container:{name}",
            sequence=self._STEP_SEQUENCE[name],
            step_kind=f"container-{name}",
            affected_cluster_id=target.cluster_id,
            affected_assignment_id=target.assignment_id,
            affected_node_id=target.node_id,
        )

    def _record_started_step(self, plan: PlanRecord, target: ManagedContainerTarget, name: str) -> None:
        step = self._step(plan, target, name)
        if step.state == MaintenanceStepState.PENDING:
            step = self.repository.transition_step(step.id, step.state_revision, MaintenanceStepState.EXECUTING)
        self.repository.record_checkpoint(
            plan_id=plan.id,
            step_id=step.id,
            checkpoint_key=f"container:{name}:intent",
            sequence=self._STEP_SEQUENCE[name],
            side_effect_state=SideEffectState.MAY_HAVE_STARTED,
            payload={"assignment_id": target.assignment_id, "unit": target.unit},
        )

    def _record_verified_step(
        self,
        plan: PlanRecord,
        target: ManagedContainerTarget,
        name: str,
        observation: dict,
    ) -> None:
        step = self._step(plan, target, name)
        if step.state == MaintenanceStepState.PENDING:
            step = self.repository.transition_step(step.id, step.state_revision, MaintenanceStepState.EXECUTING)
        if step.state == MaintenanceStepState.EXECUTING:
            self.repository.transition_step(
                step.id,
                step.state_revision,
                MaintenanceStepState.VERIFIED,
                after_observation=observation,
            )
        self.repository.record_checkpoint(
            plan_id=plan.id,
            step_id=step.id,
            checkpoint_key=f"container:{name}:verified",
            sequence=self._STEP_SEQUENCE[name] + 1,
            side_effect_state=SideEffectState.VERIFIED,
            payload={"assignment_id": target.assignment_id, "unit": target.unit},
            observation=observation,
        )

    def _recover(
        self,
        plan: PlanRecord,
        target: ManagedContainerTarget,
        ticket: MaintenanceActionTicket,
        username: str,
        phase: str,
        error: Exception,
    ) -> None:
        self._progress(ticket.run_id, "Container maintenance requires recovery.\n")
        state = self.repository.get_assignment_state(target.assignment_id)
        if state.active_plan_id == plan.id and state.workflow_state != MaintenanceWorkflowState.RECOVERY_REQUIRED:
            try:
                self.repository.transition_assignment_state(
                    target.assignment_id,
                    state.state_revision,
                    MaintenanceWorkflowState.RECOVERY_REQUIRED,
                    plan.id,
                )
            except Exception:
                pass
        current = self.repository.get_plan(plan.id)
        if current.lifecycle_state in {MaintenanceState.EXECUTING, MaintenanceState.READY}:
            try:
                self.execution.fail(ticket, error_category=f"container-{phase}-unconfirmed")
            except Exception:
                pass
        self._audit(plan, username, "container-maintenance-recovery-required", target, {"phase": phase})

    def _progress(self, run_id: int, message: str) -> None:
        if self.progress is not None:
            self.progress(run_id, message)

    def _durable_boundary(self) -> None:
        """Commit durable intent before invoking an injected remote adapter."""

        self.repository.connection.commit()

    def _audit(
        self,
        plan: PlanRecord,
        username: str,
        action: str,
        target: ManagedContainerTarget,
        detail: dict,
    ) -> None:
        self.repository.record_audit(
            username=username,
            action=action,
            cluster_id=target.cluster_id,
            item_id=plan.id,
            detail={"assignment_id": target.assignment_id, "unit": target.unit, **detail},
        )


__all__ = [
    "AllocationGuard",
    "AssignmentCompanionReconciler",
    "ContainerMaintenanceError",
    "ContainerMaintenanceService",
    "ContainerRuntime",
    "ControllerManagedWorkloadRuntime",
    "ManagedContainerTarget",
    "RuntimeActionResult",
    "resolve_managed_container_target",
]
