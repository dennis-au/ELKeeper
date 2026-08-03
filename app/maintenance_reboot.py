from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Protocol, Sequence

from pydantic import BaseModel

from .maintenance_elasticsearch import (
    AllocationCleanupTrigger,
    AllocationGuardCheckpoint,
    AllocationGuardController,
)
from .maintenance_executor import HostExecutorResult, SignedHostExecutorManifest
from .maintenance_lifecycle import MaintenanceState, SideEffectState
from .maintenance_store import CheckpointRecord, MaintenanceRepository, PlanRecord


class ControlAction(str, Enum):
    NONE = "none"
    PAUSE = "pause"
    CANCEL = "cancel"


class RebootOrchestrationStatus(str, Enum):
    BLOCKED = "blocked"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    RECOVERY_REQUIRED = "recovery_required"
    READY_FOR_POST_RETURN = "ready_for_post_return"


class ExecutorDiscoveryState(str, Enum):
    NOT_FOUND = "not_found"
    STAGED = "staged"
    RUNNING = "running"
    COMPLETE = "complete"
    RECOVERY_REQUIRED = "recovery_required"


class InvocationAmbiguous(RuntimeError):
    """The controller cannot prove whether the reboot request reached the host."""


@dataclass(frozen=True)
class PredicateDecision:
    identifier: str
    passed: bool
    evidence: str

    def __post_init__(self) -> None:
        if not self.identifier.strip():
            raise ValueError("predicate identifier must not be blank")
        if not self.evidence.strip():
            raise ValueError("predicate evidence must not be blank")


@dataclass(frozen=True)
class PredicateEvaluation:
    evaluated_at: datetime
    decisions: tuple[PredicateDecision, ...]

    def __post_init__(self) -> None:
        _aware(self.evaluated_at, "evaluated_at")
        identifiers = [item.identifier for item in self.decisions]
        if not identifiers or len(identifiers) != len(set(identifiers)):
            raise ValueError("predicate evaluation must contain unique decisions")

    @property
    def blocking_ids(self) -> tuple[str, ...]:
        return tuple(item.identifier for item in self.decisions if not item.passed)


@dataclass(frozen=True)
class ExecutorStageReceipt:
    operation_id: str
    manifest_hash: str
    acknowledged: bool
    staged_at: datetime

    def __post_init__(self) -> None:
        _aware(self.staged_at, "staged_at")


@dataclass(frozen=True)
class RebootInvocationReceipt:
    operation_id: str
    invocation_id: str
    acknowledged: bool
    acknowledged_at: datetime

    def __post_init__(self) -> None:
        if not self.operation_id.strip():
            raise ValueError("operation_id must not be blank")
        if not self.invocation_id.strip():
            raise ValueError("invocation_id must not be blank")
        _aware(self.acknowledged_at, "acknowledged_at")


@dataclass(frozen=True)
class SshDisconnectObservation:
    disconnected: bool
    observed_at: datetime

    def __post_init__(self) -> None:
        _aware(self.observed_at, "observed_at")


@dataclass(frozen=True)
class ReconnectObservation:
    connected: bool
    boot_id: str | None
    observed_at: datetime

    def __post_init__(self) -> None:
        _aware(self.observed_at, "observed_at")
        if self.connected and not self.boot_id:
            raise ValueError("a connected host observation requires a boot ID")


@dataclass(frozen=True)
class ExecutorDiscovery:
    operation_id: str
    state: ExecutorDiscoveryState
    observed_at: datetime
    result: HostExecutorResult | None = None

    def __post_init__(self) -> None:
        _aware(self.observed_at, "observed_at")
        if self.state == ExecutorDiscoveryState.COMPLETE and self.result is None:
            raise ValueError("complete executor discovery requires a validated result")
        if self.result is not None and self.result.operation_id != self.operation_id:
            raise ValueError("executor result operation identity does not match discovery")


class ClusterGuardProtocol(Protocol):
    async def capture(self, *, plan_id: str, cluster_id: int) -> Any: ...

    async def activate(self, checkpoint: Any) -> Any: ...

    async def restore(self, checkpoint: Any, *, trigger: Any) -> Any: ...


class PredicateEvaluatorProtocol(Protocol):
    async def evaluate(self, *, plan_id: str, node_id: int, stage: str) -> PredicateEvaluation: ...


class ExecutorGatewayProtocol(Protocol):
    async def stage(self, envelope: SignedHostExecutorManifest) -> ExecutorStageReceipt: ...

    async def discover(self, *, operation_id: str) -> ExecutorDiscovery: ...


class RebootGatewayProtocol(Protocol):
    async def invoke_reboot(self, *, node_id: int, operation_id: str) -> RebootInvocationReceipt: ...

    async def wait_for_disconnect(
        self, *, node_id: int, invocation_id: str,
    ) -> SshDisconnectObservation: ...

    async def wait_for_reconnect(self, *, node_id: int) -> ReconnectObservation: ...


class RebootControlProtocol(Protocol):
    def action_at(self, checkpoint: str) -> ControlAction: ...


@dataclass(frozen=True)
class ClusterGuardSpec:
    cluster_id: int
    guard: ClusterGuardProtocol | None = None

    def __post_init__(self) -> None:
        if self.cluster_id < 1:
            raise ValueError("cluster_id must be positive")


@dataclass(frozen=True)
class RebootRequest:
    plan_id: str
    node_id: int
    pre_reboot_boot_id: str
    executor_manifest: SignedHostExecutorManifest
    clusters: tuple[ClusterGuardSpec, ...]

    def __post_init__(self) -> None:
        if self.node_id < 1:
            raise ValueError("node_id must be positive")
        if not self.pre_reboot_boot_id.strip():
            raise ValueError("pre_reboot_boot_id must not be blank")
        manifest = self.executor_manifest.manifest
        if manifest.plan_id != self.plan_id or manifest.node_id != self.node_id:
            raise ValueError("executor manifest identity must match the reboot request")
        if manifest.pre_reboot_boot_id != self.pre_reboot_boot_id:
            raise ValueError("executor manifest boot ID must match the reboot request")
        identifiers = [item.cluster_id for item in self.clusters]
        if identifiers != sorted(identifiers) and len(identifiers) == len(set(identifiers)):
            object.__setattr__(self, "clusters", tuple(sorted(self.clusters, key=lambda item: item.cluster_id)))
            identifiers = sorted(identifiers)
        if len(identifiers) > 99 or len(identifiers) != len(set(identifiers)):
            raise ValueError("reboot requests require unique affected clusters")


@dataclass(frozen=True)
class RebootOrchestrationResult:
    status: RebootOrchestrationStatus
    plan_id: str
    operation_id: str
    reason_code: str
    boot_id: str | None = None
    executor_state: ExecutorDiscoveryState | None = None


@dataclass(frozen=True)
class RecoveryHandoff:
    plan_id: str
    latest_checkpoint: str | None
    side_effect_state: SideEffectState | None
    resume_allowed: bool
    observation_required: bool
    reason_code: str


@dataclass(frozen=True)
class ClusterGuardActivation:
    active: Mapping[int, Any]
    status: RebootOrchestrationStatus | None = None
    reason_code: str | None = None


class RebootControl:
    """Deterministic testable pause/cancel requests keyed by safe checkpoint."""

    def __init__(self, actions: Mapping[str, ControlAction | str] | None = None):
        self._actions = {
            key: ControlAction(value) for key, value in (actions or {}).items()
        }

    def action_at(self, checkpoint: str) -> ControlAction:
        return self._actions.get(checkpoint, ControlAction.NONE)

    def clear(self, checkpoint: str) -> None:
        self._actions.pop(checkpoint, None)


class MaintenanceRepositoryRebootJournal:
    """Append-only P2.4 journal adapter over the existing maintenance repository."""

    def __init__(self, repository: MaintenanceRepository):
        self.repository = repository

    def get(self, plan_id: str, key: str) -> CheckpointRecord | None:
        return next(
            (item for item in self.repository.list_checkpoints(plan_id) if item.checkpoint_key == key),
            None,
        )

    def latest(self, plan_id: str) -> CheckpointRecord | None:
        return self.repository.latest_checkpoint(plan_id)

    def record(
        self,
        *,
        plan_id: str,
        key: str,
        sequence: int,
        side_effect_state: SideEffectState,
        payload: Mapping[str, Any],
        observation: Mapping[str, Any] | None = None,
    ) -> CheckpointRecord:
        return self.repository.record_checkpoint(
            plan_id=plan_id,
            checkpoint_key=key,
            sequence=sequence,
            side_effect_state=side_effect_state,
            payload=payload,
            observation=observation,
        )

    def record_cluster_preparation(
        self,
        *,
        plan_id: str,
        captures: Sequence[tuple[int, Mapping[str, Any]]],
    ) -> CheckpointRecord:
        connection = self.repository.connection
        connection.execute("SAVEPOINT reboot_cluster_preparation")
        try:
            for offset, (cluster_id, capture) in enumerate(captures):
                self.record(
                    plan_id=plan_id,
                    key=f"reboot.cluster.{cluster_id}.captured",
                    sequence=100 + offset,
                    side_effect_state=SideEffectState.NOT_STARTED,
                    payload={"cluster_id": cluster_id, "capture": capture},
                )
            aggregate = self.record(
                plan_id=plan_id,
                key="reboot.clusters-prepared",
                sequence=200,
                side_effect_state=SideEffectState.PREPARED,
                payload={"cluster_ids": [item[0] for item in captures]},
            )
            connection.execute("RELEASE SAVEPOINT reboot_cluster_preparation")
            return aggregate
        except Exception:
            connection.execute("ROLLBACK TO SAVEPOINT reboot_cluster_preparation")
            connection.execute("RELEASE SAVEPOINT reboot_cluster_preparation")
            raise


class RebootOrchestrator:
    """Unwired Phase 2 reboot contract; remote effects exist only behind injected gateways."""

    SAFE_CONTROL_CHECKPOINTS = frozenset({"clusters-prepared"})

    def __init__(
        self,
        *,
        repository: MaintenanceRepository,
        predicates: PredicateEvaluatorProtocol,
        executor: ExecutorGatewayProtocol,
        host: RebootGatewayProtocol,
        control: RebootControlProtocol | None = None,
        execution_enabled: bool = False,
    ):
        self.repository = repository
        self.journal = MaintenanceRepositoryRebootJournal(repository)
        self.predicates = predicates
        self.executor = executor
        self.host = host
        self.control = control or RebootControl()
        self.execution_enabled = execution_enabled

    async def run(self, request: RebootRequest, *, resume: bool = False) -> RebootOrchestrationResult:
        if not self.execution_enabled:
            raise RuntimeError("maintenance reboot execution is disabled")
        operation_id = request.executor_manifest.manifest.operation_id
        plan = self.repository.get_plan(request.plan_id)
        self._validate_plan(plan, request)
        completed = self.journal.get(request.plan_id, "reboot.return-discovered")
        if completed is not None:
            return self._completed_result(request, completed)

        if plan.lifecycle_state == MaintenanceState.RECOVERY_REQUIRED:
            handoff = self.recovery_handoff(request.plan_id)
            return self._result(
                request,
                RebootOrchestrationStatus.RECOVERY_REQUIRED,
                handoff.reason_code,
            )
        plan = self._enter_execution(plan, resume=resume)
        if plan.lifecycle_state == MaintenanceState.PAUSED:
            return self._result(request, RebootOrchestrationStatus.PAUSED, "pause-requested")

        intent = self.journal.get(request.plan_id, "reboot.intent")
        acknowledged = self.journal.get(request.plan_id, "reboot.invocation-acknowledged")
        if intent is not None and acknowledged is None:
            self._mark_recovery_required(request.plan_id)
            return self._result(
                request,
                RebootOrchestrationStatus.RECOVERY_REQUIRED,
                "reboot-invocation-ambiguous",
            )

        if acknowledged is None:
            try:
                captures = await self._prepare_clusters(request)
            except Exception:
                self._mark_failed(request.plan_id)
                return self._result(
                    request,
                    RebootOrchestrationStatus.BLOCKED,
                    "cluster-preparation-capture-failed",
                )
            controlled = self._apply_safe_control(request, "clusters-prepared")
            if controlled is not None:
                return controlled

            try:
                prepare_evaluation = await self._evaluate(request, stage="prepare")
            except Exception:
                self._mark_failed(request.plan_id)
                return self._result(
                    request,
                    RebootOrchestrationStatus.BLOCKED,
                    "preparation-predicate-evaluation-failed",
                )
            if prepare_evaluation.blocking_ids:
                self._mark_failed(request.plan_id)
                return self._result(
                    request,
                    RebootOrchestrationStatus.BLOCKED,
                    "preparation-predicate-blocked",
                )

            activation = await self._activate_cluster_guards(request, captures)
            if activation.status is not None:
                if activation.status == RebootOrchestrationStatus.RECOVERY_REQUIRED:
                    self._mark_recovery_required(request.plan_id)
                else:
                    self._mark_failed(request.plan_id)
                return self._result(
                    request,
                    activation.status,
                    activation.reason_code or "cluster-preparation-failed",
                )
            active_guards = activation.active
            try:
                staged = await self._stage_executor(request)
            except Exception:
                restoration_ok = await self._restore_guards(request, active_guards, trigger="failure")
                self._mark_recovery_required(request.plan_id)
                return self._result(
                    request,
                    RebootOrchestrationStatus.RECOVERY_REQUIRED,
                    "executor-stage-failed" if restoration_ok else "allocation-restoration-unverified",
                )
            if not staged.acknowledged:
                restoration_ok = await self._restore_guards(request, active_guards, trigger="failure")
                self._mark_recovery_required(request.plan_id)
                return self._result(
                    request,
                    RebootOrchestrationStatus.RECOVERY_REQUIRED,
                    (
                        "executor-stage-unacknowledged"
                        if restoration_ok
                        else "allocation-restoration-unverified"
                    ),
                )

            try:
                reboot_evaluation = await self._evaluate(request, stage="reboot")
            except Exception:
                restoration_ok = await self._restore_guards(request, active_guards, trigger="failure")
                self._mark_recovery_required(request.plan_id)
                return self._result(
                    request,
                    RebootOrchestrationStatus.RECOVERY_REQUIRED,
                    (
                        "reboot-predicate-evaluation-failed"
                        if restoration_ok
                        else "allocation-restoration-unverified"
                    ),
                )
            if reboot_evaluation.blocking_ids:
                restoration_ok = await self._restore_guards(request, active_guards, trigger="failure")
                if restoration_ok:
                    self._mark_failed(request.plan_id)
                    return self._result(
                        request,
                        RebootOrchestrationStatus.BLOCKED,
                        "reboot-predicate-blocked",
                    )
                self._mark_recovery_required(request.plan_id)
                return self._result(
                    request,
                    RebootOrchestrationStatus.RECOVERY_REQUIRED,
                    "allocation-restoration-unverified",
                )

            self.journal.record(
                plan_id=request.plan_id,
                key="reboot.intent",
                sequence=700,
                side_effect_state=SideEffectState.PREPARED,
                payload={"operation_id": operation_id, "node_id": request.node_id},
                observation={"pre_reboot_boot_id": request.pre_reboot_boot_id},
            )
            try:
                invocation = await self.host.invoke_reboot(
                    node_id=request.node_id,
                    operation_id=operation_id,
                )
            except Exception:
                self._mark_recovery_required(request.plan_id)
                return self._result(
                    request,
                    RebootOrchestrationStatus.RECOVERY_REQUIRED,
                    "reboot-invocation-ambiguous",
                )
            if not invocation.acknowledged:
                self._mark_recovery_required(request.plan_id)
                return self._result(
                    request,
                    RebootOrchestrationStatus.RECOVERY_REQUIRED,
                    "reboot-invocation-unacknowledged",
                )
            if invocation.operation_id != operation_id:
                self._mark_recovery_required(request.plan_id)
                return self._result(
                    request,
                    RebootOrchestrationStatus.RECOVERY_REQUIRED,
                    "reboot-invocation-identity-mismatch",
                )
            acknowledged = self.journal.record(
                plan_id=request.plan_id,
                key="reboot.invocation-acknowledged",
                sequence=800,
                side_effect_state=SideEffectState.MAY_HAVE_STARTED,
                payload=_jsonable(invocation),
            )

        disconnected = self.journal.get(request.plan_id, "reboot.ssh-disconnected")
        if disconnected is None:
            invocation_id = str(acknowledged.payload["invocation_id"])
            try:
                disconnect = await self.host.wait_for_disconnect(
                    node_id=request.node_id,
                    invocation_id=invocation_id,
                )
            except Exception:
                self._mark_recovery_required(request.plan_id)
                return self._result(
                    request,
                    RebootOrchestrationStatus.RECOVERY_REQUIRED,
                    "ssh-disconnect-observation-failed",
                )
            disconnected = self.journal.record(
                plan_id=request.plan_id,
                key="reboot.ssh-disconnected",
                sequence=900,
                side_effect_state=SideEffectState.MAY_HAVE_STARTED,
                payload=_jsonable(disconnect),
            )
            if not disconnect.disconnected:
                self._mark_recovery_required(request.plan_id)
                return self._result(
                    request,
                    RebootOrchestrationStatus.RECOVERY_REQUIRED,
                    "ssh-disconnect-not-observed",
                )

        reconnected = self.journal.get(request.plan_id, "reboot.host-reconnected")
        if reconnected is None:
            try:
                reconnect = await self.host.wait_for_reconnect(node_id=request.node_id)
            except Exception:
                self._mark_recovery_required(request.plan_id)
                return self._result(
                    request,
                    RebootOrchestrationStatus.RECOVERY_REQUIRED,
                    "host-return-observation-failed",
                )
            reconnected = self.journal.record(
                plan_id=request.plan_id,
                key="reboot.host-reconnected",
                sequence=1000,
                side_effect_state=SideEffectState.MAY_HAVE_STARTED,
                payload=_jsonable(reconnect),
            )
        reconnect_payload = reconnected.payload
        boot_id = reconnect_payload.get("boot_id")
        if not reconnect_payload.get("connected"):
            self._mark_recovery_required(request.plan_id)
            return self._result(request, RebootOrchestrationStatus.RECOVERY_REQUIRED, "host-not-returned")
        if boot_id == request.pre_reboot_boot_id:
            self._mark_recovery_required(request.plan_id)
            return self._result(
                request,
                RebootOrchestrationStatus.RECOVERY_REQUIRED,
                "boot-id-unchanged",
                boot_id=str(boot_id),
            )

        try:
            discovery = await self.executor.discover(operation_id=operation_id)
        except Exception:
            self._mark_recovery_required(request.plan_id)
            return self._result(
                request,
                RebootOrchestrationStatus.RECOVERY_REQUIRED,
                "executor-discovery-failed",
                boot_id=str(boot_id),
            )
        if discovery.state != ExecutorDiscoveryState.COMPLETE:
            self._mark_recovery_required(request.plan_id)
            return self._result(
                request,
                RebootOrchestrationStatus.RECOVERY_REQUIRED,
                "executor-state-unverified",
                boot_id=str(boot_id),
                executor_state=discovery.state,
            )
        completed = self.journal.record(
            plan_id=request.plan_id,
            key="reboot.return-discovered",
            sequence=1100,
            side_effect_state=SideEffectState.VERIFIED,
            payload={
                "operation_id": operation_id,
                "boot_id": boot_id,
                "executor_state": discovery.state.value,
                "discovery": _jsonable(discovery),
            },
        )
        return self._completed_result(request, completed)

    def recovery_handoff(self, plan_id: str) -> RecoveryHandoff:
        latest = self.journal.latest(plan_id)
        if latest is None:
            return RecoveryHandoff(
                plan_id=plan_id,
                latest_checkpoint=None,
                side_effect_state=None,
                resume_allowed=True,
                observation_required=False,
                reason_code="no-side-effect-checkpoint",
            )
        if latest.checkpoint_key == "reboot.return-discovered":
            return RecoveryHandoff(
                plan_id=plan_id,
                latest_checkpoint=latest.checkpoint_key,
                side_effect_state=latest.side_effect_state,
                resume_allowed=False,
                observation_required=False,
                reason_code="continue-post-return-verification",
            )
        ambiguous = latest.checkpoint_key == "reboot.intent" or latest.side_effect_state in {
            SideEffectState.MAY_HAVE_STARTED,
            SideEffectState.VERIFIED,
        }
        return RecoveryHandoff(
            plan_id=plan_id,
            latest_checkpoint=latest.checkpoint_key,
            side_effect_state=latest.side_effect_state,
            resume_allowed=not ambiguous,
            observation_required=ambiguous,
            reason_code=(
                "observe-host-and-executor-before-resume"
                if ambiguous
                else "safe-pre-side-effect-checkpoint"
            ),
        )

    async def _prepare_clusters(self, request: RebootRequest) -> dict[int, Any]:
        prepared = self.journal.get(request.plan_id, "reboot.clusters-prepared")
        if prepared is not None:
            return {
                item.cluster_id: self._load_capture(request.plan_id, item)
                for item in request.clusters
            }
        captures: list[tuple[int, Mapping[str, Any]]] = []
        hydrated: dict[int, Any] = {}
        for item in request.clusters:
            if item.guard is None:
                raw: Any = {"cluster_id": item.cluster_id, "guard_required": False}
            else:
                raw = await item.guard.capture(plan_id=request.plan_id, cluster_id=item.cluster_id)
            serialized = _jsonable(raw)
            captures.append((item.cluster_id, serialized))
            hydrated[item.cluster_id] = raw
        self.journal.record_cluster_preparation(plan_id=request.plan_id, captures=captures)
        return hydrated

    def _load_capture(self, plan_id: str, item: ClusterGuardSpec) -> Any:
        checkpoint = self.journal.get(plan_id, f"reboot.cluster.{item.cluster_id}.captured")
        if checkpoint is None:
            raise RuntimeError("aggregate cluster preparation checkpoint is incomplete")
        capture = checkpoint.payload["capture"]
        if isinstance(item.guard, AllocationGuardController):
            return AllocationGuardCheckpoint.model_validate(capture)
        return capture

    async def _evaluate(self, request: RebootRequest, *, stage: str) -> PredicateEvaluation:
        evaluation = await self.predicates.evaluate(
            plan_id=request.plan_id,
            node_id=request.node_id,
            stage=stage,
        )
        prefix = f"reboot.{stage}-predicates."
        attempt = 1 + sum(
            item.checkpoint_key.startswith(prefix)
            for item in self.repository.list_checkpoints(request.plan_id)
        )
        if attempt > 99:
            raise RuntimeError("predicate re-evaluation attempt limit exceeded")
        self.journal.record(
            plan_id=request.plan_id,
            key=f"{prefix}{attempt}",
            sequence=(300 if stage == "prepare" else 600) + attempt,
            side_effect_state=SideEffectState.NOT_STARTED if stage == "prepare" else SideEffectState.PREPARED,
            payload=_jsonable(evaluation),
        )
        return evaluation

    async def _activate_cluster_guards(
        self,
        request: RebootRequest,
        captures: Mapping[int, Any],
    ) -> ClusterGuardActivation:
        existing = self.journal.get(request.plan_id, "reboot.cluster-guards-active")
        if existing is not None:
            return ClusterGuardActivation(active={
                item.cluster_id: self._hydrate_checkpoint(
                    item.guard,
                    existing.payload["guards"][str(item.cluster_id)],
                )
                for item in request.clusters if item.guard is not None
            })
        active: dict[int, Any] = {}
        for item in request.clusters:
            if item.guard is None:
                continue
            try:
                result = await item.guard.activate(captures[item.cluster_id])
            except Exception:
                await self._restore_guards(request, active, trigger="failure")
                return ClusterGuardActivation(
                    active=active,
                    status=RebootOrchestrationStatus.RECOVERY_REQUIRED,
                    reason_code="cluster-preparation-activation-ambiguous",
                )
            status = _field(result, "status")
            checkpoint = _field(result, "checkpoint")
            if status != "active":
                restored = await self._restore_guards(request, active, trigger="failure")
                if status == "recovery_required" or not restored:
                    return ClusterGuardActivation(
                        active=active,
                        status=RebootOrchestrationStatus.RECOVERY_REQUIRED,
                        reason_code="cluster-preparation-recovery-required",
                    )
                return ClusterGuardActivation(
                    active=active,
                    status=RebootOrchestrationStatus.BLOCKED,
                    reason_code="cluster-preparation-failed",
                )
            active[item.cluster_id] = checkpoint
        self.journal.record(
            plan_id=request.plan_id,
            key="reboot.cluster-guards-active",
            sequence=400,
            side_effect_state=SideEffectState.VERIFIED,
            payload={"guards": {str(key): _jsonable(value) for key, value in active.items()}},
        )
        return ClusterGuardActivation(active=active)

    async def _restore_guards(
        self,
        request: RebootRequest,
        active: Mapping[int, Any],
        *,
        trigger: str,
    ) -> bool:
        verified = True
        results: dict[str, Any] = {}
        for item in reversed(request.clusters):
            if item.cluster_id not in active or item.guard is None:
                continue
            trigger_value: Any = (
                AllocationCleanupTrigger(trigger)
                if isinstance(item.guard, AllocationGuardController)
                else trigger
            )
            try:
                result = await item.guard.restore(active[item.cluster_id], trigger=trigger_value)
                results[str(item.cluster_id)] = _jsonable(result)
                verified = verified and _field(result, "status") == "restored"
            except Exception:
                verified = False
                results[str(item.cluster_id)] = {
                    "status": "recovery_required",
                    "error_category": "allocation-restoration-failed",
                }
        self.journal.record(
            plan_id=request.plan_id,
            key=f"reboot.cluster-guards-restored-{trigger}",
            sequence=650,
            side_effect_state=SideEffectState.VERIFIED if verified else SideEffectState.MAY_HAVE_STARTED,
            payload={"verified": verified, "results": results},
        )
        return verified

    async def _stage_executor(self, request: RebootRequest) -> ExecutorStageReceipt:
        existing = self.journal.get(request.plan_id, "reboot.executor-staged")
        if existing is not None:
            return ExecutorStageReceipt(
                operation_id=str(existing.payload["operation_id"]),
                manifest_hash=str(existing.payload["manifest_hash"]),
                acknowledged=bool(existing.payload["acknowledged"]),
                staged_at=_datetime(existing.payload["staged_at"]),
            )
        receipt = await self.executor.stage(request.executor_manifest)
        if receipt.operation_id != request.executor_manifest.manifest.operation_id:
            raise RuntimeError("executor stage receipt operation identity does not match")
        if receipt.manifest_hash != request.executor_manifest.signature.payload_sha256:
            raise RuntimeError("executor stage receipt manifest digest does not match")
        self.journal.record(
            plan_id=request.plan_id,
            key="reboot.executor-staged",
            sequence=500,
            side_effect_state=SideEffectState.VERIFIED if receipt.acknowledged else SideEffectState.MAY_HAVE_STARTED,
            payload=_jsonable(receipt),
        )
        return receipt

    def _apply_safe_control(
        self,
        request: RebootRequest,
        checkpoint: str,
    ) -> RebootOrchestrationResult | None:
        if checkpoint not in self.SAFE_CONTROL_CHECKPOINTS:
            raise ValueError("pause and cancel may be evaluated only at a safe checkpoint")
        action = self.control.action_at(checkpoint)
        if action == ControlAction.NONE:
            return None
        plan = self.repository.get_plan(request.plan_id)
        prefix = f"reboot.control.{action.value}."
        attempt = 1 + sum(
            item.checkpoint_key.startswith(prefix)
            for item in self.repository.list_checkpoints(request.plan_id)
        )
        self.journal.record(
            plan_id=request.plan_id,
            key=f"{prefix}{attempt}",
            sequence=250 + attempt,
            side_effect_state=SideEffectState.NOT_STARTED,
            payload={"safe_checkpoint": checkpoint},
        )
        paused = self.repository.transition_plan(
            request.plan_id,
            plan.state_revision,
            MaintenanceState.PAUSED,
        )
        if action == ControlAction.PAUSE:
            return self._result(request, RebootOrchestrationStatus.PAUSED, "pause-requested")
        self.repository.transition_plan(
            request.plan_id,
            paused.state_revision,
            MaintenanceState.CANCELLED,
        )
        return self._result(request, RebootOrchestrationStatus.CANCELLED, "cancel-requested")

    def _enter_execution(self, plan: PlanRecord, *, resume: bool) -> PlanRecord:
        if plan.lifecycle_state == MaintenanceState.READY:
            return self.repository.transition_plan(
                plan.id,
                plan.state_revision,
                MaintenanceState.EXECUTING,
            )
        if plan.lifecycle_state == MaintenanceState.PAUSED and resume:
            return self.repository.transition_plan(
                plan.id,
                plan.state_revision,
                MaintenanceState.EXECUTING,
            )
        if plan.lifecycle_state in {MaintenanceState.EXECUTING, MaintenanceState.PAUSED}:
            return plan
        raise RuntimeError(f"maintenance plan cannot execute from {plan.lifecycle_state.value}")

    @staticmethod
    def _validate_plan(plan: PlanRecord, request: RebootRequest) -> None:
        if plan.operation_kind != "reboot" or plan.target_node_id != request.node_id:
            raise ValueError("maintenance plan identity does not match the reboot request")

    def _mark_failed(self, plan_id: str) -> None:
        plan = self.repository.get_plan(plan_id)
        if plan.lifecycle_state == MaintenanceState.EXECUTING:
            self.repository.transition_plan(plan_id, plan.state_revision, MaintenanceState.FAILED)

    def _mark_recovery_required(self, plan_id: str) -> None:
        plan = self.repository.get_plan(plan_id)
        if plan.lifecycle_state in {MaintenanceState.EXECUTING, MaintenanceState.PAUSED}:
            self.repository.transition_plan(
                plan_id,
                plan.state_revision,
                MaintenanceState.RECOVERY_REQUIRED,
            )

    def _completed_result(
        self,
        request: RebootRequest,
        checkpoint: CheckpointRecord,
    ) -> RebootOrchestrationResult:
        return self._result(
            request,
            RebootOrchestrationStatus.READY_FOR_POST_RETURN,
            "return-discovered",
            boot_id=str(checkpoint.payload["boot_id"]),
            executor_state=ExecutorDiscoveryState(str(checkpoint.payload["executor_state"])),
        )

    @staticmethod
    def _result(
        request: RebootRequest,
        status: RebootOrchestrationStatus,
        reason_code: str,
        *,
        boot_id: str | None = None,
        executor_state: ExecutorDiscoveryState | None = None,
    ) -> RebootOrchestrationResult:
        return RebootOrchestrationResult(
            status=status,
            plan_id=request.plan_id,
            operation_id=request.executor_manifest.manifest.operation_id,
            reason_code=reason_code,
            boot_id=boot_id,
            executor_state=executor_state,
        )

    @staticmethod
    def _hydrate_checkpoint(guard: ClusterGuardProtocol | None, payload: Any) -> Any:
        if isinstance(guard, AllocationGuardController):
            return AllocationGuardCheckpoint.model_validate(payload)
        return payload


def _field(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value[name]
    return getattr(value, name)


def _aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone")
    return value.astimezone(timezone.utc)


def _datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return _aware(value, "timestamp")
    return _aware(datetime.fromisoformat(str(value).replace("Z", "+00:00")), "timestamp")


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return _aware(value, "timestamp").isoformat().replace("+00:00", "Z")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported reboot journal value: {type(value).__name__}")
