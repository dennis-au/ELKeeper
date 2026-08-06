"""Signed reboot bridge for the host-maintenance workflow.

The existing reboot orchestrator owns durable reboot checkpoints, executor
staging, reconnect observation, and ambiguity handling. This module binds that
contract to a host workflow after its selected managed workloads are stopped.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Protocol

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .container_maintenance import ManagedContainerTarget, RuntimeActionResult
from .executor import (
    HostExecutorManifest,
    SignedHostExecutorManifest,
    executor_instance_unit,
    executor_paths,
    sign_executor_manifest,
)
from .lifecycle import SideEffectState
from .planned_contracts import MaintenanceWorkflowState
from .post_return import ExecutorCleanupTarget
from .reboot import (
    PredicateDecision,
    PredicateEvaluation,
    RebootOrchestrationStatus,
    RebootOrchestrator,
    RebootRequest,
)
from .runtime import ControllerManagedHostRuntime
from .store import MaintenanceRepository, PlanRecord, parse_timestamp


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class HostMaintenanceRebootError(RuntimeError):
    """A signed host reboot could not reach a verified return boundary."""


class HostRebootRuntime(Protocol):
    async def read_boot_id(self, node_id: int) -> str | None: ...

    async def cleanup_executor(self, target: ExecutorCleanupTarget): ...


class HostMaintenanceRebootPredicates:
    """Recheck only the durable host-workflow boundary before reboot dispatch."""

    def __init__(
        self,
        repository: MaintenanceRepository,
        *,
        expected_assignment_ids: tuple[int, ...],
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.repository = repository
        self.expected_assignment_ids = expected_assignment_ids
        self.clock = clock

    async def evaluate(self, *, plan_id: str, node_id: int, stage: str) -> PredicateEvaluation:
        plan = self.repository.get_plan(plan_id)
        host = self.repository.get_host_state(node_id)
        host_ready = (
            plan.target_node_id == node_id
            and host.active_plan_id == plan_id
            and host.workflow_state == MaintenanceWorkflowState.MAINTENANCE
        )
        assignments_ready = all(
            state.active_plan_id == plan_id
            and state.workflow_state == MaintenanceWorkflowState.MAINTENANCE
            for assignment_id in self.expected_assignment_ids
            for state in (self.repository.get_assignment_state(assignment_id),)
        )
        return PredicateEvaluation(
            evaluated_at=self.clock(),
            decisions=(
                PredicateDecision(
                    identifier=f"host-workflow-maintenance:{stage}",
                    passed=host_ready,
                    evidence="host workflow is in the verified maintenance state",
                ),
                PredicateDecision(
                    identifier=f"host-workloads-stopped:{stage}",
                    passed=assignments_ready,
                    evidence="every selected managed workload remains stopped by this plan",
                ),
            ),
        )


class HostMaintenanceRebootCoordinator:
    """Create a persisted signed request and delegate all reboot effects."""

    _REQUEST_CHECKPOINT = "host-reboot-request"
    _REQUEST_SEQUENCE = 4900

    def __init__(
        self,
        repository: MaintenanceRepository,
        *,
        runtime: ControllerManagedHostRuntime,
        orchestrator: RebootOrchestrator,
        signing_key: Ed25519PrivateKey,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.repository = repository
        self.runtime = runtime
        self.orchestrator = orchestrator
        self.signing_key = signing_key
        self.clock = clock

    async def reboot(
        self,
        *,
        plan: PlanRecord,
        targets: tuple[ManagedContainerTarget, ...],
    ) -> RuntimeActionResult:
        request = await self._request(plan, targets)
        result = await self.orchestrator.run(request)
        if result.status != RebootOrchestrationStatus.READY_FOR_POST_RETURN:
            raise HostMaintenanceRebootError(
                f"host reboot requires recovery ({result.reason_code})"
            )
        return RuntimeActionResult(confirmed=True, detail="signed-host-reboot-verified")

    async def cleanup(self, *, plan: PlanRecord) -> RuntimeActionResult:
        request = self._load_request(plan)
        if self._checkpoint(plan.id, "reboot.return-discovered") is None:
            raise HostMaintenanceRebootError("host reboot return was not verified")
        paths = executor_paths(request.executor_manifest.manifest.operation_id)
        proof = await self.runtime.cleanup_executor(
            ExecutorCleanupTarget(
                operation_id=request.executor_manifest.manifest.operation_id,
                unit=executor_instance_unit(request.executor_manifest.manifest.operation_id),
                paths=(
                    str(paths.manifest),
                    str(paths.public_key),
                    str(paths.checkpoint),
                    str(paths.result),
                ),
            )
        )
        if not proof.proven:
            raise HostMaintenanceRebootError("host reboot executor cleanup was not confirmed")
        return RuntimeActionResult(confirmed=True, detail="signed-host-reboot-cleanup")

    async def _request(
        self,
        plan: PlanRecord,
        targets: tuple[ManagedContainerTarget, ...],
    ) -> RebootRequest:
        existing = self._checkpoint(plan.id, self._REQUEST_CHECKPOINT)
        if existing is not None:
            return self._request_from_payload(plan, existing.payload)
        if plan.target_node_id is None:
            raise HostMaintenanceRebootError("host maintenance plan has no target node")
        boot_id = await self.runtime.read_boot_id(plan.target_node_id)
        if boot_id is None:
            raise HostMaintenanceRebootError("host boot identity is unavailable before reboot")
        now = self._aware(self.clock())
        expires_at = min(parse_timestamp(plan.expires_at), now + timedelta(hours=1))
        if expires_at <= now:
            raise HostMaintenanceRebootError("host maintenance plan expired before reboot")
        operation_id = plan.id
        paths = executor_paths(operation_id)
        manifest = HostExecutorManifest(
            operation_id=operation_id,
            plan_id=plan.id,
            node_id=plan.target_node_id,
            created_at=now,
            expires_at=expires_at,
            pre_reboot_boot_id=boot_id,
            required_units=tuple(sorted(target.unit for target in targets)),
            checkpoint_path=str(paths.checkpoint),
            result_path=str(paths.result),
        )
        envelope = sign_executor_manifest(manifest, self.signing_key)
        request = RebootRequest(
            plan_id=plan.id,
            node_id=plan.target_node_id,
            pre_reboot_boot_id=boot_id,
            executor_manifest=envelope,
            clusters=(),
        )
        self.repository.record_checkpoint(
            plan_id=plan.id,
            checkpoint_key=self._REQUEST_CHECKPOINT,
            sequence=self._REQUEST_SEQUENCE,
            side_effect_state=SideEffectState.PREPARED,
            payload={"request": self._request_payload(request)},
        )
        return request

    def _load_request(self, plan: PlanRecord) -> RebootRequest:
        checkpoint = self._checkpoint(plan.id, self._REQUEST_CHECKPOINT)
        if checkpoint is None:
            raise HostMaintenanceRebootError("host reboot executor was never staged")
        return self._request_from_payload(plan, checkpoint.payload)

    @staticmethod
    def _request_payload(request: RebootRequest) -> dict:
        return {
            "plan_id": request.plan_id,
            "node_id": request.node_id,
            "pre_reboot_boot_id": request.pre_reboot_boot_id,
            "executor_manifest": request.executor_manifest.model_dump(mode="json"),
            "clusters": [],
        }

    @staticmethod
    def _request_from_payload(plan: PlanRecord, payload: object) -> RebootRequest:
        if not isinstance(payload, dict) or not isinstance(payload.get("request"), dict):
            raise HostMaintenanceRebootError("persisted host reboot request is invalid")
        try:
            raw = payload["request"]
            request = RebootRequest(
                plan_id=raw["plan_id"],
                node_id=raw["node_id"],
                pre_reboot_boot_id=raw["pre_reboot_boot_id"],
                executor_manifest=SignedHostExecutorManifest.model_validate(raw["executor_manifest"]),
                clusters=(),
            )
        except (TypeError, ValueError) as error:
            raise HostMaintenanceRebootError("persisted host reboot request is invalid") from error
        if request.plan_id != plan.id or request.node_id != plan.target_node_id:
            raise HostMaintenanceRebootError("persisted host reboot request targets another plan")
        return request

    def _checkpoint(self, plan_id: str, key: str):
        return next(
            (item for item in self.repository.list_checkpoints(plan_id) if item.checkpoint_key == key),
            None,
        )

    @staticmethod
    def _aware(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("host reboot clock must include a timezone")
        return value.astimezone(timezone.utc)


__all__ = [
    "HostMaintenanceRebootCoordinator",
    "HostMaintenanceRebootError",
    "HostMaintenanceRebootPredicates",
]
