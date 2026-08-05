"""Fail-closed, checkpointed planning for one workload maintenance action.

This module owns maintenance plan/checkpoint composition only.  It deliberately
does not import orchestration adapters or workload implementation details.
Phase 3 execution remains unavailable until the rolling-restart capability is
approved in the release artifact and a role-specific adapter is assembled.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from app.modules.platform import capability_snapshot

from .lifecycle import MaintenanceState, SideEffectState
from .store import CheckpointRecord, MaintenanceRepository, PlanRecord, StepRecord
from .workload_contracts import (
    ReadinessEvidence,
    WorkloadMaintenancePlanInput,
    WorkloadMaintenanceTarget,
    WorkloadOperation,
    rollback_allowed,
    validate_readiness,
)


WORKLOAD_EXECUTION_CAPABILITY = "rolling_restart"
_DEFAULT_TTL = timedelta(minutes=30)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _operation_kind(operation: WorkloadOperation) -> str:
    return {
        WorkloadOperation.RESTART: "workload_restart",
        WorkloadOperation.RESOURCE_CHANGE: "resource_change",
        WorkloadOperation.CERTIFICATE_ROTATION: "workload_certificate_rotation",
        WorkloadOperation.CONFIGURATION_CHANGE: "workload_configuration_change",
    }[operation]


def _target_payload(target: WorkloadMaintenanceTarget) -> dict[str, Any]:
    return target.model_dump(mode="json")


def _steps(target: WorkloadMaintenanceTarget) -> tuple[dict[str, str], ...]:
    return (
        {"key": "acquire-locks", "kind": "acquire-maintenance-locks"},
        {"key": "refresh-observation", "kind": "refresh-workload-observation"},
        {"key": "validate-readiness", "kind": "validate-role-readiness"},
        {"key": "workload-side-effect", "kind": "maintain-one-workload"},
        {"key": "verify-readiness", "kind": "verify-role-readiness"},
        {"key": "release-locks", "kind": "release-maintenance-locks"},
    )


def _execution_blockers(target: WorkloadMaintenanceTarget) -> tuple[str, ...]:
    blockers = []
    if not target.budget.safe:
        blockers.append(target.budget.reason)
    if not capability_snapshot().get(WORKLOAD_EXECUTION_CAPABILITY, False):
        blockers.append("rolling_restart_capability_disabled")
    return tuple(blockers)


class WorkloadMaintenancePlanService:
    """Maintenance-owned plan/checkpoint service for exactly one assignment."""

    def __init__(self, repository: MaintenanceRepository, *, clock=_now) -> None:
        self.repository = repository
        self.clock = clock

    def create_preview(
        self,
        request: WorkloadMaintenancePlanInput,
        *,
        requested_by: str,
        expires_at: datetime | None = None,
        observation: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Persist a non-mutating single-workload plan and initial checkpoint."""

        target = request.target
        existing = self.repository.get_plan_by_idempotency_key(request.idempotency_key)
        if existing is not None:
            return self.progress(existing.id)

        blockers = _execution_blockers(target)
        plan = {
            "kind": "workload_maintenance",
            "reason": request.reason.strip(),
            "target": _target_payload(target),
            "execution_enabled": False,
            "execution_blockers": list(blockers),
            "rollback": {
                "automatic_after_process_started": rollback_allowed(target.role, process_started=True),
                "elasticsearch_no_automatic_downgrade": target.role.value == "elasticsearch",
            },
            "steps": list(_steps(target)),
        }
        record = self.repository.create_plan(
            operation_kind=_operation_kind(target.operation),
            plan=plan,
            idempotency_key=request.idempotency_key,
            requested_by=requested_by,
            expires_at=expires_at or (self.clock() + _DEFAULT_TTL),
            observation={"workload": dict(observation or {})},
            target_node_id=target.node_id,
            target_cluster_id=target.cluster_id,
            target_assignment_id=target.assignment_id,
            initial_state=MaintenanceState.BLOCKED,
            target_manifest={
                "assignment_id": target.assignment_id,
                "expected_image": target.expected_image,
                "expected_digest": target.expected_digest,
                "execution_enabled": False,
            },
        )
        created_steps = self._create_steps(record, target)
        side_effect_step = next(item for item in created_steps if item.step_key == "workload-side-effect")
        self.repository.record_checkpoint(
            plan_id=record.id,
            step_id=side_effect_step.id,
            checkpoint_key="workload-side-effect",
            sequence=1,
            side_effect_state=SideEffectState.NOT_STARTED,
            payload={
                "target": _target_payload(target),
                "before_artifact": {
                    "image": target.expected_image,
                    "digest": target.expected_digest,
                },
                "rollback_allowed_after_process_started": rollback_allowed(target.role, process_started=True),
            },
        )
        self.repository.record_audit(
            username=requested_by,
            action="maintenance-workload-plan-preview-created",
            cluster_id=target.cluster_id,
            item_id=record.id,
            detail={
                "assignment_id": target.assignment_id,
                "operation": target.operation.value,
                "execution_enabled": False,
                "blockers": list(blockers),
            },
        )
        return self.progress(record.id)

    def progress(self, plan_id: str) -> dict[str, Any]:
        """Return a read-only projection reusable by Roles, Dashboard and topology."""

        record = self.repository.get_plan(plan_id)
        target_value = record.plan.get("target", {})
        target = WorkloadMaintenanceTarget.model_validate(target_value)
        steps = self.repository.list_steps(plan_id)
        checkpoints = self.repository.list_checkpoints(plan_id)
        latest = checkpoints[-1] if checkpoints else None
        return {
            "plan_id": record.id,
            "operation": record.operation_kind,
            "lifecycle_state": record.lifecycle_state.value,
            "assignment_id": target.assignment_id,
            "cluster_id": target.cluster_id,
            "node_id": target.node_id,
            "role": target.role.value,
            "execution_enabled": False,
            "execution_blockers": list(record.plan.get("execution_blockers", ())),
            "rollback": dict(record.plan.get("rollback", {})),
            "step_count": len(steps),
            "verified_steps": sum(1 for item in steps if item.state.value == "verified"),
            "checkpoint": self._checkpoint_projection(latest),
        }

    def progress_for_assignments(self, assignment_ids: tuple[int, ...]) -> dict[int, dict[str, Any]]:
        """Project the newest workload-maintenance state for visible workloads.

        This is a read-only public projection for workload, dashboard, and
        topology presenters.  It deliberately has no route dependency so
        callers can adopt it without coupling those modules to maintenance
        storage.
        """

        requested = {int(item) for item in assignment_ids if int(item) > 0}
        if not requested:
            return {}
        result: dict[int, dict[str, Any]] = {}
        for record in self.repository.list_plans(limit=500):
            if record.plan.get("kind") != "workload_maintenance":
                continue
            assignment_id = record.target_assignment_id
            if assignment_id not in requested or assignment_id in result:
                continue
            result[assignment_id] = self.progress(record.id)
        return result

    def observe_checkpoint(
        self,
        plan_id: str,
        evidence: ReadinessEvidence,
        *,
        process_started: bool,
    ) -> dict[str, Any]:
        """Classify a future adapter observation without performing an action.

        This supports restart recovery and makes the Elasticsearch rollback
        boundary durable before any executor is enabled.
        """

        checkpoint = self._side_effect_checkpoint(plan_id)
        payload_target = checkpoint.payload.get("target", {})
        target = WorkloadMaintenanceTarget.model_validate(payload_target)
        ready, reason = validate_readiness(target, evidence)
        if ready:
            classification, resumable = "complete", False
        elif process_started and not rollback_allowed(target.role, process_started=True):
            classification, resumable, reason = "recovery_required", False, "elasticsearch_no_automatic_downgrade"
        elif not evidence.identity_matches:
            classification, resumable, reason = "recovery_required", False, "workload_identity_mismatch"
        else:
            classification, resumable = "incomplete", True
        self.repository.persist_startup_classification(
            checkpoint.id,
            expected_revision=checkpoint.classification_revision,
            classification=classification,
            reason_code=reason,
            resumable=resumable,
            evidence={
                "role": target.role.value,
                "process_started": process_started,
                "ready": evidence.ready,
                "identity_matches": evidence.identity_matches,
                "observed_at": evidence.observed_at,
            },
            now=self.clock(),
        )
        return self.progress(plan_id)

    def _create_steps(self, record: PlanRecord, target: WorkloadMaintenanceTarget) -> tuple[StepRecord, ...]:
        return tuple(
            self.repository.create_step(
                plan_id=record.id,
                step_key=item["key"],
                sequence=index,
                step_kind=item["kind"],
                affected_cluster_id=target.cluster_id,
                affected_assignment_id=target.assignment_id,
                affected_node_id=target.node_id,
            )
            for index, item in enumerate(_steps(target), start=1)
        )

    def _side_effect_checkpoint(self, plan_id: str) -> CheckpointRecord:
        checkpoint = next(
            (item for item in self.repository.list_checkpoints(plan_id) if item.checkpoint_key == "workload-side-effect"),
            None,
        )
        if checkpoint is None:
            raise ValueError("Workload maintenance side-effect checkpoint is unavailable")
        return checkpoint

    @staticmethod
    def _checkpoint_projection(checkpoint: CheckpointRecord | None) -> dict[str, Any] | None:
        if checkpoint is None:
            return None
        return {
            "key": checkpoint.checkpoint_key,
            "side_effect_state": checkpoint.side_effect_state.value,
            "recovery_classification": checkpoint.recovery_classification,
            "recovery_reason": checkpoint.recovery_reason_code,
            "resumable": checkpoint.resumable,
        }


def workload_maintenance_progress_in_connection(
    connection,
    assignment_ids: tuple[int, ...],
) -> dict[int, dict[str, Any]]:
    """Return redacted workload-plan progress for compatibility presenters.

    This is intentionally a read-only public seam.  It lets cluster, topology,
    dashboard, and action-console presenters surface durable maintenance state
    without importing maintenance storage implementation details or enabling a
    workload executor.
    """

    return WorkloadMaintenancePlanService(MaintenanceRepository(connection)).progress_for_assignments(
        assignment_ids
    )


__all__ = [
    "WORKLOAD_EXECUTION_CAPABILITY",
    "WorkloadMaintenancePlanService",
    "workload_maintenance_progress_in_connection",
]
