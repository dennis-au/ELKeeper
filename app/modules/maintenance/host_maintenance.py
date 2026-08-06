"""Host-scoped planned maintenance composed from exact managed workloads.

The signed host-action adapter is injected at the application boundary. The
workflow remains unavailable unless its independent release capability allows
the action, and legacy operator handoff records never claim a reboot occurred.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Protocol

from app.modules.workloads import WorkloadRepository

from .container_maintenance import (
    AllocationGuard,
    AssignmentCompanionReconciler,
    ContainerRuntime,
    ManagedContainerTarget,
    RuntimeActionResult,
    resolve_managed_container_target,
)
from .execution import (
    AdapterResult,
    MaintenanceAction,
    MaintenanceActionTicket,
    MaintenanceAdapterRequest,
    MaintenanceExecutionService,
)
from .lifecycle import HostMaintenanceState, MaintenanceState, MaintenanceStepState, SideEffectState
from .planned_contracts import MaintenanceWorkflowState
from .runtime import ControllerMaintenanceIO
from .store import MaintenanceRepository, PlanRecord
from .models import MaintenancePolicy
from .post_return import (
    HostMaintenancePostReturnRequest,
    HostMaintenancePostReturnResult,
    PostReturnExpectations,
    WorkloadExpectation,
)


class HostMaintenanceError(RuntimeError):
    """Host maintenance could not reach a verified durable boundary."""


class HostReturnRuntime(ContainerRuntime, Protocol):
    async def host_ready(self, node_id: int) -> RuntimeActionResult: ...


class HostPostReturnVerifier(Protocol):
    async def verify_host_maintenance(
        self,
        request: HostMaintenancePostReturnRequest,
    ) -> HostMaintenancePostReturnResult: ...


class HostRebootExecutor(Protocol):
    """Run and clean the signed reboot executor for one prepared host."""

    async def reboot(
        self,
        *,
        plan: PlanRecord,
        targets: tuple[ManagedContainerTarget, ...],
    ) -> RuntimeActionResult: ...

    async def cleanup(self, *, plan: PlanRecord) -> RuntimeActionResult: ...


HostRebootExecutorFactory = Callable[
    [PlanRecord, tuple[ManagedContainerTarget, ...]],
    HostRebootExecutor,
]


class ControllerManagedHostMaintenanceRuntime:
    """Validate host return prerequisites through controller-owned SSH only."""

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

    async def host_ready(self, node_id: int) -> RuntimeActionResult:
        connected = await self.io.wait_for_ssh(node_id=node_id, timeout_seconds=30)
        if not connected:
            return RuntimeActionResult(confirmed=False, detail="host-ssh-unavailable")
        podman_ready = await self.io.podman_socket_ready(node_id=node_id)
        quadlet_ready = await self.io.quadlet_generator_ready(node_id=node_id)
        boot_id = await self.io.read_boot_id(node_id=node_id)
        return RuntimeActionResult(
            confirmed=podman_ready and quadlet_ready and boot_id is not None,
            detail="host-runtime-rediscovered",
        )

    async def wait_for_ssh(self, node_id: int, timeout_seconds: int) -> bool:
        return await self.io.wait_for_ssh(node_id=node_id, timeout_seconds=timeout_seconds)

    async def podman_socket_ready(self, node_id: int) -> bool:
        return await self.io.podman_socket_ready(node_id=node_id)

    async def quadlet_generator_ready(self, node_id: int) -> bool:
        return await self.io.quadlet_generator_ready(node_id=node_id)

    async def generated_units(self, node_id: int, units: tuple[str, ...]) -> frozenset[str]:
        return await self.io.generated_units(node_id=node_id, units=units)

    async def unit_states(self, node_id: int, units: tuple[str, ...]):
        return await self.io.unit_states(node_id=node_id, units=units)

    async def endpoint_ready(self, node_id: int, endpoint_ref: str) -> bool:
        return await self.io.endpoint_ready(node_id=node_id, endpoint_ref=endpoint_ref)


class ControllerManagedServiceAvailability:
    """Count only active managed units through controller-owned SSH."""

    def __init__(self, connection, io: ControllerMaintenanceIO) -> None:
        self.connection = connection
        self.io = io

    async def available(self, expectation) -> int:
        assignments = WorkloadRepository.from_connection(self.connection).active_for_cluster_in_connection(
            self.connection,
            expectation.cluster_id,
        )
        targets = tuple(
            resolve_managed_container_target(self.connection, int(item["id"]))
            for item in assignments
            if item["role"] == expectation.role
        )
        by_node: dict[int, list[str]] = {}
        for target in targets:
            by_node.setdefault(target.node_id, []).append(target.unit)
        available = 0
        for node_id, units in by_node.items():
            states = await self.io.unit_states(node_id=node_id, units=tuple(sorted(units)))
            available += sum(states.get(unit) is True for unit in units)
        return available


HostTargetResolver = Callable[[PlanRecord], tuple[ManagedContainerTarget, ...]]


def resolve_managed_container_targets_for_host(
    connection,
    plan: PlanRecord,
) -> tuple[ManagedContainerTarget, ...]:
    """Resolve the exact active target set preserved by a host preview."""

    if plan.target_node_id is None:
        raise HostMaintenanceError("Host maintenance plan has no target host")
    expected_ids = _manifest_assignment_ids(plan)
    active = WorkloadRepository.from_connection(connection).active_for_node(plan.target_node_id)
    active_ids = {int(item["id"]) for item in active}
    if active_ids != set(expected_ids):
        raise HostMaintenanceError("Host maintenance target set changed; create a new preview")
    targets = tuple(resolve_managed_container_target(connection, assignment_id) for assignment_id in expected_ids)
    if any(target.node_id != plan.target_node_id for target in targets):
        raise HostMaintenanceError("Host maintenance target is not assigned to the selected host")
    return targets


def _manifest_assignment_ids(plan: PlanRecord) -> tuple[int, ...]:
    revisions = plan.target_manifest.get("assignment_revisions", ())
    if not isinstance(revisions, (list, tuple)):
        raise HostMaintenanceError("Host maintenance plan has no valid target manifest")
    assignment_ids = []
    for item in revisions:
        if not isinstance(item, dict) or isinstance(item.get("assignment_id"), bool):
            raise HostMaintenanceError("Host maintenance plan has no valid target manifest")
        assignment_id = item.get("assignment_id")
        if not isinstance(assignment_id, int) or assignment_id < 1:
            raise HostMaintenanceError("Host maintenance plan has no valid target manifest")
        assignment_ids.append(assignment_id)
    if not assignment_ids or len(assignment_ids) != len(set(assignment_ids)):
        raise HostMaintenanceError("Host maintenance plan has no valid target manifest")
    return tuple(sorted(assignment_ids))


def _stop_priority(target: ManagedContainerTarget) -> tuple[int, int]:
    role = target.role
    priority = {
        "elastic-agent": 10,
        "filebeat": 10,
        "metricbeat": 10,
        "kibana": 20,
        "fleet-server": 20,
        "logstash": 20,
        "coordinating": 30,
        "ingest": 30,
        "ml": 30,
        "hot": 40,
        "warm": 40,
        "cold": 40,
        "frozen": 40,
        "content": 40,
        "master": 50,
    }.get(role, 25)
    return priority, target.assignment_id


def _start_priority(target: ManagedContainerTarget) -> tuple[int, int]:
    role = target.role
    priority = {
        "master": 10,
        "hot": 20,
        "warm": 20,
        "cold": 20,
        "frozen": 20,
        "content": 20,
        "coordinating": 30,
        "ingest": 30,
        "ml": 30,
        "kibana": 40,
        "fleet-server": 40,
        "logstash": 40,
        "elastic-agent": 50,
        "filebeat": 50,
        "metricbeat": 50,
    }.get(role, 35)
    return priority, target.assignment_id


class HostMaintenanceService:
    """Prepare, reboot, and return one host without touching other hosts."""

    def __init__(
        self,
        repository: MaintenanceRepository,
        *,
        execution: MaintenanceExecutionService,
        targets_resolver: HostTargetResolver | None = None,
        runtime: HostReturnRuntime,
        companions: AssignmentCompanionReconciler,
        allocation_guard: AllocationGuard | None = None,
        post_return: HostPostReturnVerifier | None = None,
        reboot_executor: HostRebootExecutor | None = None,
        reboot_executor_factory: HostRebootExecutorFactory | None = None,
        progress: Callable[[int, str], None] | None = None,
    ) -> None:
        if reboot_executor is not None and reboot_executor_factory is not None:
            raise ValueError("Host maintenance accepts one reboot executor source")
        self.repository = repository
        self.execution = execution
        self.targets_resolver = targets_resolver or (
            lambda plan: resolve_managed_container_targets_for_host(self.repository.connection, plan)
        )
        self.runtime = runtime
        self.companions = companions
        self.allocation_guard = allocation_guard
        self.post_return = post_return
        self.reboot_executor = reboot_executor
        self.reboot_executor_factory = reboot_executor_factory
        self.progress = progress

    async def prepare(self, plan_id: str, *, username: str):
        plan, targets = self._plan_and_targets(plan_id)
        self._post_return_expectations(plan, targets)
        if self.post_return is None:
            raise HostMaintenanceError("Host maintenance post-return verification is not configured")
        if any(target.data_bearing for target in targets) and self.allocation_guard is None:
            raise HostMaintenanceError("Data-bearing host maintenance requires an allocation guard")
        ticket = self.execution.prepare(plan.id, MaintenanceAction.EXECUTE, username=username)
        try:
            self._progress(ticket.run_id, "Preparing managed workloads on the selected host.\n")
            host = self.repository.get_host_state(plan.target_node_id)
            host = self.repository.transition_host_state(
                plan.target_node_id,
                host.state_revision,
                HostMaintenanceState.PLANNING,
                plan.id,
            )
            self._prepare_assignments(plan, targets, ticket.run_id)
            self._record_step(plan, "prepare", None, SideEffectState.VERIFIED, {"run_id": ticket.run_id})
            for cluster_id in sorted({target.cluster_id for target in targets if target.data_bearing}):
                assert self.allocation_guard is not None
                self._durable_boundary()
                await self.allocation_guard.capture(plan_id=plan.id, cluster_id=cluster_id)
                self._durable_boundary()
                await self.allocation_guard.activate(plan_id=plan.id, cluster_id=cluster_id)
            host = self.repository.transition_host_workflow_state(
                plan.target_node_id,
                host.workflow_state_revision,
                MaintenanceWorkflowState.READY_TO_STOP,
            )
            self._audit(plan, username, "host-maintenance-ready-to-stop", {"run_id": ticket.run_id})
            return host
        except Exception as error:
            self._recover(plan, targets, ticket, username, "prepare", error)
            raise

    async def stop_workloads(self, plan_id: str, *, username: str):
        plan, targets = self._plan_and_targets(plan_id)
        host = self._require_host_state(plan, MaintenanceWorkflowState.READY_TO_STOP)
        ticket = self._ticket(plan, username)
        try:
            host = self.repository.transition_host_workflow_state(
                plan.target_node_id,
                host.workflow_state_revision,
                MaintenanceWorkflowState.STOPPING,
            )
            self._progress(ticket.run_id, "Stopping managed workloads on the selected host.\n")
            for index, target in enumerate(sorted(targets, key=_stop_priority), start=1):
                state = self._require_assignment_state(plan, target, MaintenanceWorkflowState.READY_TO_STOP)
                state = self.repository.transition_assignment_state(
                    target.assignment_id,
                    state.state_revision,
                    MaintenanceWorkflowState.STOPPING,
                    plan.id,
                )
                self._record_step(plan, "stop", target, SideEffectState.MAY_HAVE_STARTED, {}, index=index)
                self._durable_boundary()
                self._confirmed(await self.runtime.stop(target), "stop")
                self._record_step(plan, "stop-complete", target, SideEffectState.VERIFIED, {"unit": target.unit}, index=index)
                self.repository.transition_assignment_state(
                    target.assignment_id,
                    state.state_revision,
                    MaintenanceWorkflowState.MAINTENANCE,
                    plan.id,
                )
            host = self.repository.transition_host_workflow_state(
                plan.target_node_id,
                host.workflow_state_revision,
                MaintenanceWorkflowState.MAINTENANCE,
            )
            host = self.repository.transition_host_state(
                plan.target_node_id,
                host.state_revision,
                HostMaintenanceState.MAINTENANCE,
                plan.id,
            )
            self._audit(plan, username, "host-maintenance-workloads-stopped", {})
            return host
        except Exception as error:
            self._recover(plan, targets, ticket, username, "stop", error)
            raise

    async def record_operator_handoff(self, plan_id: str, *, username: str):
        """Compatibility-only marker for plans created before host reboot execution.

        New host maintenance actions use :meth:`reboot_host`; this retained
        marker never claims that a reboot occurred.
        """

        plan, targets = self._plan_and_targets(plan_id)
        host = self._require_host_state(plan, MaintenanceWorkflowState.MAINTENANCE)
        ticket = self._ticket(plan, username)
        self._record_step(plan, "operator-handoff", None, SideEffectState.VERIFIED, {"operator_handoff": True})
        self._audit(plan, username, "host-maintenance-operator-handoff", {"host_action_executed": False})
        self._progress(ticket.run_id, "Host maintenance is ready for the operator handoff.\n")
        return host

    async def reboot_host(self, plan_id: str, *, username: str):
        """Execute the explicitly approved signed reboot after workloads stop."""

        plan, targets = self._plan_and_targets(plan_id)
        host = self._require_host_state(plan, MaintenanceWorkflowState.MAINTENANCE)
        reboot_executor = self._reboot_executor_for(plan, targets)
        ticket = self._ticket(plan, username)
        try:
            self._record_step(plan, "reboot", None, SideEffectState.MAY_HAVE_STARTED, {})
            self._progress(ticket.run_id, "Staging the signed host reboot executor.\n")
            self._durable_boundary()
            self._confirmed(
                await reboot_executor.reboot(plan=plan, targets=targets),
                "reboot",
            )
            self._record_step(plan, "reboot-complete", None, SideEffectState.VERIFIED, {})
            self._audit(plan, username, "host-maintenance-reboot-complete", {"run_id": ticket.run_id})
            self._progress(ticket.run_id, "Host reboot and boot transition were verified.\n")
            return host
        except Exception as error:
            self._recover(plan, targets, ticket, username, "reboot", error)
            raise

    async def return_to_service(self, plan_id: str, *, username: str):
        plan, targets = self._plan_and_targets(plan_id)
        host = self._require_host_state(plan, MaintenanceWorkflowState.MAINTENANCE)
        ticket = self._ticket(plan, username)
        try:
            host = self.repository.transition_host_workflow_state(
                plan.target_node_id,
                host.workflow_state_revision,
                MaintenanceWorkflowState.RETURNING,
            )
            self._record_step(plan, "host-rediscovery", None, SideEffectState.MAY_HAVE_STARTED, {})
            self._progress(ticket.run_id, "Rediscovering the selected host before workload return.\n")
            self._durable_boundary()
            self._confirmed(await self.runtime.host_ready(plan.target_node_id), "host rediscovery")
            self._record_step(plan, "host-rediscovery-complete", None, SideEffectState.VERIFIED, {"node_id": plan.target_node_id})
            self._progress(ticket.run_id, "Returning managed workloads on the selected host to service.\n")
            for index, target in enumerate(sorted(targets, key=_start_priority), start=1):
                state = self._require_assignment_state(plan, target, MaintenanceWorkflowState.MAINTENANCE)
                state = self.repository.transition_assignment_state(
                    target.assignment_id,
                    state.state_revision,
                    MaintenanceWorkflowState.RETURNING,
                    plan.id,
                )
                self._record_step(plan, "return", target, SideEffectState.MAY_HAVE_STARTED, {}, index=index)
                self._durable_boundary()
                self._confirmed(await self.runtime.start(target), "start")
                self._record_step(plan, "return-complete", target, SideEffectState.VERIFIED, {"unit": target.unit}, index=index)
                state = self.repository.transition_assignment_state(
                    target.assignment_id,
                    state.state_revision,
                    MaintenanceWorkflowState.VERIFYING,
                    plan.id,
                )
                self._durable_boundary()
                self._confirmed(await self.runtime.ready(target), "readiness")
                self._durable_boundary()
                self._confirmed(
                    await self.companions.reconcile(assignment_id=target.assignment_id, run_id=ticket.run_id),
                    "companion reconciliation",
                )
                self._record_step(plan, "verification", target, SideEffectState.VERIFIED, {"unit": target.unit}, index=index)
            for cluster_id in sorted({target.cluster_id for target in targets if target.data_bearing}):
                assert self.allocation_guard is not None
                self._durable_boundary()
                await self.allocation_guard.restore(plan_id=plan.id, cluster_id=cluster_id)
            self._progress(ticket.run_id, "Verifying the returned host and affected clusters.\n")
            verification_request = self._post_return_request(plan, targets)
            self._record_step(
                plan,
                "post-return-verification-started",
                None,
                SideEffectState.MAY_HAVE_STARTED,
                {},
            )
            self._durable_boundary()
            verification = await self.post_return.verify_host_maintenance(verification_request)
            self._record_step(
                plan,
                "post-return-verification",
                None,
                (
                    SideEffectState.VERIFIED
                    if verification.state == "complete"
                    else SideEffectState.MAY_HAVE_STARTED
                ),
                verification.model_dump(mode="json"),
            )
            if verification.state != "complete":
                raise HostMaintenanceError("Host maintenance post-return verification requires recovery")
            if self._reboot_completed(plan):
                reboot_executor = self._reboot_executor_for(plan, targets)
                self._record_step(
                    plan,
                    "executor-cleanup",
                    None,
                    SideEffectState.MAY_HAVE_STARTED,
                    {},
                )
                self._durable_boundary()
                self._confirmed(
                    await reboot_executor.cleanup(plan=plan),
                    "executor cleanup",
                )
                self._record_step(
                    plan,
                    "executor-cleanup-complete",
                    None,
                    SideEffectState.VERIFIED,
                    {},
                )
            self.execution.finalize(ticket, AdapterResult(lifecycle_state=MaintenanceState.SUCCEEDED))
            self._complete_assignments(plan, targets, ticket.run_id)
            host = self.repository.transition_host_state(
                plan.target_node_id,
                host.state_revision,
                HostMaintenanceState.AVAILABLE,
                None,
            )
            self._audit(plan, username, "host-maintenance-returned", {"run_id": ticket.run_id})
            return host
        except Exception as error:
            self._recover(plan, targets, ticket, username, "return", error)
            raise

    async def aclose(self) -> None:
        """Release action-scoped transport resources without changing state."""

        close = getattr(self.post_return, "aclose", None)
        if close is not None:
            result = close()
            if inspect.isawaitable(result):
                await result
        close = getattr(self.allocation_guard, "aclose", None)
        if close is not None:
            result = close()
            if inspect.isawaitable(result):
                await result
        close = getattr(self.reboot_executor, "aclose", None)
        if close is not None:
            result = close()
            if inspect.isawaitable(result):
                await result

    def _reboot_executor_for(
        self,
        plan: PlanRecord,
        targets: tuple[ManagedContainerTarget, ...],
    ) -> HostRebootExecutor:
        if self.reboot_executor is not None:
            return self.reboot_executor
        if self.reboot_executor_factory is not None:
            return self.reboot_executor_factory(plan, targets)
        raise HostMaintenanceError("Host maintenance reboot execution is not configured")

    def _reboot_completed(self, plan: PlanRecord) -> bool:
        return any(
            checkpoint.checkpoint_key == "host:reboot-complete:host"
            for checkpoint in self.repository.list_checkpoints(plan.id)
        )

    def _plan_and_targets(self, plan_id: str) -> tuple[PlanRecord, tuple[ManagedContainerTarget, ...]]:
        plan = self.repository.get_plan(plan_id)
        if plan.operation_kind != "reboot" or plan.target_node_id is None:
            raise HostMaintenanceError("Maintenance plan is not a host-maintenance plan")
        if plan.target_manifest.get("public_operation") != "host_maintenance":
            raise HostMaintenanceError("Maintenance plan was not previewed for host maintenance")
        targets = tuple(self.targets_resolver(plan))
        expected_ids = _manifest_assignment_ids(plan)
        if tuple(sorted(target.assignment_id for target in targets)) != expected_ids:
            raise HostMaintenanceError("Host maintenance target set does not match the approved preview")
        if any(target.node_id != plan.target_node_id for target in targets):
            raise HostMaintenanceError("Host maintenance target set includes another host")
        return plan, targets

    @staticmethod
    def _post_return_expectations(
        plan: PlanRecord,
        targets: tuple[ManagedContainerTarget, ...],
    ) -> PostReturnExpectations:
        raw = plan.target_manifest.get("post_return_expectations")
        if not isinstance(raw, dict):
            raise HostMaintenanceError(
                "Host maintenance plan has no immutable post-return expectations"
            )
        try:
            expectations = PostReturnExpectations.model_validate(raw)
        except ValueError as error:
            raise HostMaintenanceError(
                "Host maintenance plan has invalid post-return expectations"
            ) from error
        expected_clusters = {target.cluster_id for target in targets}
        observed_clusters = {item.cluster_id for item in expectations.clusters}
        if observed_clusters != expected_clusters:
            raise HostMaintenanceError(
                "Host maintenance post-return expectations do not cover every affected cluster"
            )
        expected_es_assignments = {
            target.assignment_id for target in targets
            if target.role in {"master", "hot", "warm", "ml", "ingest", "coordinating"}
        }
        observed_es_assignments = {
            node.assignment_id
            for cluster in expectations.clusters
            for node in cluster.nodes
        }
        if observed_es_assignments != expected_es_assignments:
            raise HostMaintenanceError(
                "Host maintenance post-return expectations do not cover every affected Elasticsearch node"
            )
        return expectations

    def _post_return_request(
        self,
        plan: PlanRecord,
        targets: tuple[ManagedContainerTarget, ...],
    ) -> HostMaintenancePostReturnRequest:
        expectations = self._post_return_expectations(plan, targets)
        try:
            policy = MaintenancePolicy.model_validate(plan.plan.get("policy") or {})
        except (AttributeError, ValueError) as error:
            raise HostMaintenanceError("Host maintenance plan has no valid return policy") from error
        return HostMaintenancePostReturnRequest(
            plan_id=plan.id,
            node_id=plan.target_node_id,
            host_return_timeout_seconds=policy.host_return_timeout_seconds,
            workloads=tuple(
                WorkloadExpectation(assignment_id=target.assignment_id, unit=target.unit)
                for target in sorted(targets, key=lambda item: item.unit)
            ),
            endpoints=expectations.endpoints,
            clusters=expectations.clusters,
            service_budgets=expectations.service_budgets,
        )

    def _prepare_assignments(
        self,
        plan: PlanRecord,
        targets: tuple[ManagedContainerTarget, ...],
        run_id: int,
    ) -> None:
        repository = WorkloadRepository.from_connection(self.repository.connection)
        revisions = {
            int(item["assignment_id"]): int(item["revision"])
            for item in plan.target_manifest["assignment_revisions"]
        }
        states = []
        for target in targets:
            state = self.repository.get_assignment_state(target.assignment_id)
            if state.workflow_state != MaintenanceWorkflowState.AVAILABLE:
                raise HostMaintenanceError("Host maintenance target is already in maintenance")
            states.append((target, state))
        for target, state in states:
            self.repository.transition_assignment_state(
                target.assignment_id,
                state.state_revision,
                MaintenanceWorkflowState.PREPARING,
                plan.id,
            )
        for target in targets:
            if not repository.claim_assignment_operation_in_connection(
                self.repository.connection,
                assignment_id=target.assignment_id,
                expected_revision=revisions[target.assignment_id],
                run_id=run_id,
            ):
                raise HostMaintenanceError("Host maintenance target changed or is claimed by another operation")
        for target in targets:
            state = self.repository.get_assignment_state(target.assignment_id)
            self.repository.transition_assignment_state(
                target.assignment_id,
                state.state_revision,
                MaintenanceWorkflowState.READY_TO_STOP,
                plan.id,
            )

    def _complete_assignments(
        self,
        plan: PlanRecord,
        targets: tuple[ManagedContainerTarget, ...],
        run_id: int,
    ) -> None:
        repository = WorkloadRepository.from_connection(self.repository.connection)
        for target in targets:
            state = self._require_assignment_state(plan, target, MaintenanceWorkflowState.VERIFYING)
            self.repository.transition_assignment_state(
                target.assignment_id,
                state.state_revision,
                MaintenanceWorkflowState.AVAILABLE,
                None,
            )
            if not repository.release_assignment_operation_in_connection(
                self.repository.connection,
                assignment_id=target.assignment_id,
                run_id=run_id,
            ):
                raise HostMaintenanceError("Host maintenance workload claim could not be released")

    def _require_host_state(self, plan: PlanRecord, expected: MaintenanceWorkflowState):
        host = self.repository.get_host_state(plan.target_node_id)
        if (
            host.active_plan_id != plan.id
            or host.workflow_state != expected
        ):
            raise HostMaintenanceError(
                f"Host maintenance action is not available while the host is {host.workflow_state.value}"
            )
        return host

    def _require_assignment_state(
        self,
        plan: PlanRecord,
        target: ManagedContainerTarget,
        expected: MaintenanceWorkflowState,
    ):
        state = self.repository.get_assignment_state(target.assignment_id)
        if state.active_plan_id != plan.id or state.workflow_state != expected:
            raise HostMaintenanceError(
                f"Host maintenance action is not available while a workload is {state.workflow_state.value}"
            )
        return state

    def _ticket(self, plan: PlanRecord, username: str) -> MaintenanceActionTicket:
        if plan.run_id is None:
            raise HostMaintenanceError("Host maintenance plan has no active run")
        locks = self.repository.list_active_locks(plan.id)
        if not locks:
            raise HostMaintenanceError("Host maintenance plan no longer owns its locks")
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

    def _record_step(
        self,
        plan: PlanRecord,
        name: str,
        target: ManagedContainerTarget | None,
        side_effect_state: SideEffectState,
        observation: dict,
        *,
        index: int = 0,
    ) -> None:
        base = {
            "prepare": 100,
            "stop": 200,
            "stop-complete": 300,
            "operator-handoff": 400,
            "reboot": 450,
            "reboot-complete": 460,
            "host-rediscovery": 7000,
            "host-rediscovery-complete": 7010,
            "return": 7100,
            "return-complete": 7200,
            "verification": 7300,
            "post-return-verification-started": 7400,
            "post-return-verification": 7410,
            "executor-cleanup": 7500,
            "executor-cleanup-complete": 7510,
        }[name]
        sequence = base + index
        target_key = str(target.assignment_id) if target else "host"
        step = self.repository.create_step(
            plan_id=plan.id,
            step_key=f"host:{name}:{target_key}",
            sequence=sequence,
            step_kind=f"host-{name}",
            affected_cluster_id=target.cluster_id if target else None,
            affected_assignment_id=target.assignment_id if target else None,
            affected_node_id=plan.target_node_id,
        )
        if step.state == MaintenanceStepState.PENDING:
            step = self.repository.transition_step(step.id, step.state_revision, MaintenanceStepState.EXECUTING)
        if side_effect_state == SideEffectState.VERIFIED and step.state == MaintenanceStepState.EXECUTING:
            step = self.repository.transition_step(
                step.id,
                step.state_revision,
                MaintenanceStepState.VERIFIED,
                after_observation=observation,
            )
        self.repository.record_checkpoint(
            plan_id=plan.id,
            step_id=step.id,
            checkpoint_key=f"host:{name}:{target_key}",
            sequence=sequence,
            side_effect_state=side_effect_state,
            payload={"node_id": plan.target_node_id, **({"unit": target.unit} if target else {})},
            observation=observation,
        )

    @staticmethod
    def _confirmed(result: RuntimeActionResult, action: str) -> None:
        if not result.confirmed:
            raise HostMaintenanceError(f"Host maintenance {action} was not confirmed")

    def _recover(
        self,
        plan: PlanRecord,
        targets: tuple[ManagedContainerTarget, ...],
        ticket: MaintenanceActionTicket,
        username: str,
        phase: str,
        error: Exception,
    ) -> None:
        del error
        self._progress(ticket.run_id, "Host maintenance requires recovery.\n")
        host = self.repository.get_host_state(plan.target_node_id)
        if host.active_plan_id == plan.id and host.state != HostMaintenanceState.RECOVERY_REQUIRED:
            try:
                self.repository.transition_host_state(
                    plan.target_node_id,
                    host.state_revision,
                    HostMaintenanceState.RECOVERY_REQUIRED,
                    plan.id,
                )
            except Exception:
                pass
        for target in targets:
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
                self.execution.fail(ticket, error_category=f"host-{phase}-unconfirmed")
            except Exception:
                pass
        self._audit(plan, username, "host-maintenance-recovery-required", {"phase": phase})

    def _progress(self, run_id: int, message: str) -> None:
        if self.progress is not None:
            self.progress(run_id, message)

    def _durable_boundary(self) -> None:
        """Commit durable intent before invoking an injected remote adapter."""

        self.repository.connection.commit()

    def _audit(self, plan: PlanRecord, username: str, action: str, detail: dict) -> None:
        self.repository.record_audit(
            username=username,
            action=action,
            item_id=plan.id,
            detail={"node_id": plan.target_node_id, **detail},
        )


__all__ = [
    "HostMaintenanceError",
    "HostMaintenanceService",
    "HostPostReturnVerifier",
    "HostRebootExecutor",
    "ControllerManagedServiceAvailability",
    "HostReturnRuntime",
    "ControllerManagedHostMaintenanceRuntime",
    "resolve_managed_container_targets_for_host",
]
