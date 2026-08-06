"""Durable recovery reconciliation for planned host and container workflows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Iterable

from app.modules.platform import mark_recovery_required_in_connection

from .lifecycle import HostMaintenanceState, MaintenanceState
from .planned_contracts import MaintenanceWorkflowState
from .store import MaintenanceRepository, PlanRecord, parse_timestamp, utc_now


_PLANNED_WORKFLOW_OPERATIONS = frozenset({"host_maintenance", "container_maintenance"})


class RebootRecoveryDisposition(str, Enum):
    """The only safe next boundary for an interrupted host reboot."""

    NOT_APPLICABLE = "not_applicable"
    RESUME_PRE_REBOOT = "resume_pre_reboot"
    OBSERVE_REBOOT = "observe_reboot"
    CONTINUE_POST_RETURN = "continue_post_return"


@dataclass(frozen=True)
class WorkflowRecoveryResult:
    """Redacted result of reconciling one interrupted workflow."""

    plan_id: str
    lifecycle_state: MaintenanceState
    transitioned_plan: bool
    transitioned_host: bool
    transitioned_assignment_ids: tuple[int, ...]
    missing_assignment_ids: tuple[int, ...]
    reboot_disposition: RebootRecoveryDisposition
    reboot_checkpoint: str | None
    resume_allowed: bool
    observation_required: bool


class MaintenanceWorkflowRecoveryService:
    """Protect precise planned-workflow ownership until explicit recovery.

    The service does not start, stop, return, or clean up a workload. It only
    records that an interrupted or expired workflow needs operator recovery and
    preserves its existing locks, claims, allocation guards, and checkpoints.
    """

    def __init__(self, repository: MaintenanceRepository) -> None:
        self.repository = repository

    def expire_due_workflows(
        self,
        *,
        now: datetime | None = None,
        username: str = "system",
    ) -> tuple[WorkflowRecoveryResult, ...]:
        current_time = now or utc_now()
        return tuple(
            self.reconcile_plan(
                plan.id,
                reason="maintenance-window-expired",
                username=username,
            )
            for plan in self.repository.list_recovery_plans()
            if parse_timestamp(plan.expires_at) <= current_time
            and self._is_planned_workflow(plan)
        )

    def reconcile_startup_workflows(
        self,
        *,
        username: str = "system",
    ) -> tuple[WorkflowRecoveryResult, ...]:
        """Reconcile state after the generic startup checkpoint pass."""

        return tuple(
            self.reconcile_plan(
                plan.id,
                reason="controller-restart-rediscovery-required",
                username=username,
            )
            for plan in self.repository.list_recovery_plans()
            if self._is_planned_workflow(plan)
        )

    def reconcile_plan(
        self,
        plan_id: str,
        *,
        reason: str,
        username: str,
    ) -> WorkflowRecoveryResult:
        """Move an interrupted host/container workflow to durable recovery."""

        plan = self.repository.get_plan(plan_id)
        if not self._is_planned_workflow(plan):
            raise ValueError("Maintenance plan is not a planned host or container workflow")
        reboot_disposition, reboot_checkpoint, resume_allowed, observation_required = (
            self._reboot_recovery_disposition(plan)
        )

        transitioned_plan = False
        if plan.lifecycle_state in {MaintenanceState.EXECUTING, MaintenanceState.PAUSED}:
            plan = self.repository.transition_plan(
                plan.id,
                plan.state_revision,
                MaintenanceState.RECOVERY_REQUIRED,
            )
            transitioned_plan = True
        elif plan.lifecycle_state != MaintenanceState.RECOVERY_REQUIRED:
            raise ValueError(
                f"Maintenance workflow recovery is not available while the plan is {plan.lifecycle_state.value}"
            )

        if plan.run_id is not None:
            mark_recovery_required_in_connection(
                self.repository.connection,
                [plan.run_id],
                "Maintenance workflow requires explicit recovery before ownership can be released.",
            )

        transitioned_host = False
        if self._public_operation(plan) == "host_maintenance" and plan.target_node_id is not None:
            host = self.repository.find_host_state(plan.target_node_id)
            if host and host.active_plan_id == plan.id and host.state != HostMaintenanceState.RECOVERY_REQUIRED:
                self.repository.transition_host_state(
                    plan.target_node_id,
                    host.state_revision,
                    HostMaintenanceState.RECOVERY_REQUIRED,
                    plan.id,
                )
                transitioned_host = True

        transitioned_assignments: list[int] = []
        missing_assignments: list[int] = []
        for assignment_id in self._assignment_ids(plan):
            state = self.repository.find_assignment_state(assignment_id)
            if state is None:
                missing_assignments.append(assignment_id)
                continue
            if state.active_plan_id != plan.id or state.workflow_state == MaintenanceWorkflowState.RECOVERY_REQUIRED:
                continue
            self.repository.transition_assignment_state(
                assignment_id,
                state.state_revision,
                MaintenanceWorkflowState.RECOVERY_REQUIRED,
                plan.id,
            )
            transitioned_assignments.append(assignment_id)

        if transitioned_plan or transitioned_host or transitioned_assignments or missing_assignments:
            self.repository.record_audit(
                username=username,
                action="maintenance-workflow-recovery-required",
                cluster_id=plan.target_cluster_id,
                item_id=plan.id,
                detail={
                    "reason": reason,
                    "node_id": plan.target_node_id,
                    "transitioned_host": transitioned_host,
                    "transitioned_assignment_ids": transitioned_assignments,
                    "missing_assignment_ids": missing_assignments,
                    "reboot_disposition": reboot_disposition.value,
                    "reboot_checkpoint": reboot_checkpoint,
                    "resume_allowed": resume_allowed,
                    "observation_required": observation_required,
                },
            )
        return WorkflowRecoveryResult(
            plan_id=plan.id,
            lifecycle_state=self.repository.get_plan(plan.id).lifecycle_state,
            transitioned_plan=transitioned_plan,
            transitioned_host=transitioned_host,
            transitioned_assignment_ids=tuple(transitioned_assignments),
            missing_assignment_ids=tuple(missing_assignments),
            reboot_disposition=reboot_disposition,
            reboot_checkpoint=reboot_checkpoint,
            resume_allowed=resume_allowed,
            observation_required=observation_required,
        )

    @staticmethod
    def _public_operation(plan: PlanRecord) -> str | None:
        value = plan.target_manifest.get("public_operation")
        return value if isinstance(value, str) else None

    def _is_planned_workflow(self, plan: PlanRecord) -> bool:
        return self._public_operation(plan) in _PLANNED_WORKFLOW_OPERATIONS

    def _reboot_recovery_disposition(
        self,
        plan: PlanRecord,
    ) -> tuple[RebootRecoveryDisposition, str | None, bool, bool]:
        if self._public_operation(plan) != "host_maintenance":
            return RebootRecoveryDisposition.NOT_APPLICABLE, None, False, False
        checkpoints = {item.checkpoint_key: item for item in self.repository.list_checkpoints(plan.id)}
        if "reboot.return-discovered" in checkpoints or "host:reboot-complete:host" in checkpoints:
            checkpoint = (
                "reboot.return-discovered"
                if "reboot.return-discovered" in checkpoints
                else "host:reboot-complete:host"
            )
            return RebootRecoveryDisposition.CONTINUE_POST_RETURN, checkpoint, False, False
        for checkpoint in (
            "reboot.host-reconnected",
            "reboot.ssh-disconnected",
            "reboot.invocation-acknowledged",
            "reboot.intent",
        ):
            if checkpoint in checkpoints:
                return RebootRecoveryDisposition.OBSERVE_REBOOT, checkpoint, False, True
        for checkpoint in (
            "reboot.cluster-guards-active",
            "reboot.executor-staged",
            "reboot.clusters-prepared",
            "host-reboot-request",
            "host:reboot:host",
        ):
            if checkpoint in checkpoints:
                return RebootRecoveryDisposition.RESUME_PRE_REBOOT, checkpoint, True, False
        return RebootRecoveryDisposition.NOT_APPLICABLE, None, False, False

    @staticmethod
    def _assignment_ids(plan: PlanRecord) -> Iterable[int]:
        manifest = plan.target_manifest
        values = manifest.get("assignment_revisions", ())
        assignment_ids: list[int] = []
        if isinstance(values, (list, tuple)):
            for item in values:
                if not isinstance(item, dict):
                    continue
                assignment_id = item.get("assignment_id")
                if isinstance(assignment_id, int) and not isinstance(assignment_id, bool) and assignment_id > 0:
                    assignment_ids.append(assignment_id)
        if not assignment_ids and plan.target_assignment_id is not None:
            assignment_ids.append(plan.target_assignment_id)
        return tuple(sorted(set(assignment_ids)))


__all__ = [
    "MaintenanceWorkflowRecoveryService",
    "RebootRecoveryDisposition",
    "WorkflowRecoveryResult",
]
