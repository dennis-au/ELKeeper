"""Redacted maintenance-operation read-model serialization."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.modules.maintenance.lifecycle import HostMaintenanceState, MaintenanceState, MaintenanceStepState, SideEffectState, redact_structure
from app.modules.maintenance.planned_contracts import MaintenanceWorkflowState
from app.modules.maintenance.store import CheckpointRecord, HostStateRecord, PlanRecord, StepRecord

_STEP_STATES = {
    MaintenanceStepState.PENDING: "pending", MaintenanceStepState.EXECUTING: "active",
    MaintenanceStepState.VERIFIED: "completed", MaintenanceStepState.SKIPPED: "skipped",
    MaintenanceStepState.FAILED: "failed", MaintenanceStepState.RECOVERY_REQUIRED: "recovery_required",
}
_SAFE_CONTROL_CHECKPOINTS = frozenset({"reboot.clusters-prepared", "reboot.return-discovered"})


@dataclass(frozen=True)
class MaintenanceActionCapabilities:
    pause: bool = False
    resume: bool = False
    cancel: bool = False
    recover: bool = False


def _checkpoint_label(key: str) -> str:
    value = key.removeprefix("reboot.").replace(".", " ").replace("-", " ")
    return value[:1].upper() + value[1:]


def _safe_checkpoint(checkpoint: CheckpointRecord | None) -> tuple[bool, str]:
    if checkpoint is None:
        return True, "No protected remote side effect has started."
    if checkpoint.checkpoint_key in _SAFE_CONTROL_CHECKPOINTS:
        return True, "The latest persisted checkpoint is an operator action boundary."
    if checkpoint.side_effect_state in {SideEffectState.MAY_HAVE_STARTED, SideEffectState.VERIFIED}:
        return False, "Host or cluster side effects may still be active; rediscovery must finish first."
    return False, "The current preparation checkpoint is not an approved operator action boundary."


def _host_boot(checkpoints: Sequence[CheckpointRecord], evidence: Mapping[str, Any]) -> dict[str, Any]:
    keys = {item.checkpoint_key for item in checkpoints}
    if "reboot.return-discovered" in keys:
        state, verified = "returned", True
    elif "reboot.host-reconnected" in keys:
        state, verified = "returned", bool(evidence.get("boot_transition_verified"))
    elif "reboot.ssh-disconnected" in keys or "reboot.invocation-acknowledged" in keys:
        state, verified = "waiting_for_return", False
    elif "reboot.intent" in keys:
        state, verified = "reboot_requested", False
    else:
        state, verified = "not_started", False
    return {"state": evidence.get("state", state), "bootTransitionVerified": bool(evidence.get("boot_transition_verified", verified)), "observedAt": evidence.get("observed_at"), "detail": evidence.get("detail")}


def _executor(checkpoints: Sequence[CheckpointRecord], evidence: Mapping[str, Any]) -> dict[str, Any]:
    keys = {item.checkpoint_key for item in checkpoints}
    state = "staged" if "reboot.executor-staged" in keys else "not_staged"
    returned = next((item for item in reversed(checkpoints) if item.checkpoint_key == "reboot.return-discovered"), None)
    if returned is not None:
        discovered = str(returned.payload.get("executor_state", "unavailable"))
        state = "complete" if discovered == "complete" else discovered
    return {"state": str(evidence.get("state", state)), "signatureVerified": evidence.get("manifest_signature_verified"), "resultIdentityVerified": evidence.get("result_identity_verified"), "resultImported": evidence.get("result_imported"), "reason": evidence.get("reason"), "observedAt": evidence.get("observed_at"), "checks": list(evidence.get("checks", ()))}


def _actions(state: MaintenanceState, capabilities: MaintenanceActionCapabilities, safe_checkpoint: bool, checkpoint: CheckpointRecord | None, cleanup: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    unresolved = any(item.get("state") == "unresolved" for item in cleanup)
    at_cancel_boundary = checkpoint is None or checkpoint.checkpoint_key == "reboot.clusters-prepared"
    controls: dict[str, Any] = {}
    if state == MaintenanceState.EXECUTING and capabilities.pause:
        controls["pause"] = {"enabled": safe_checkpoint}
    if state == MaintenanceState.PAUSED and capabilities.resume:
        controls["resume"] = {"enabled": safe_checkpoint}
    if state in {MaintenanceState.DRAFT, MaintenanceState.READY, MaintenanceState.BLOCKED, MaintenanceState.EXECUTING, MaintenanceState.PAUSED, MaintenanceState.RECOVERY_REQUIRED} and capabilities.cancel:
        enabled = at_cancel_boundary and not unresolved
        controls["cancel"] = {"enabled": enabled, "reason": None if enabled else "Cancellation is blocked until active effects and temporary cluster settings are resolved."}
    if state == MaintenanceState.RECOVERY_REQUIRED and capabilities.recover:
        controls["recover"] = {"enabled": True, "requiresSafeCheckpoint": False, "reason": "Recovery begins with state rediscovery and does not assume the last command completed."}
    return controls


def serialize_maintenance_operation(
    plan: PlanRecord,
    *,
    steps: Sequence[StepRecord] = (),
    checkpoints: Sequence[CheckpointRecord] = (),
    host_state: HostStateRecord | None = None,
    workflow_state: MaintenanceWorkflowState | None = None,
    workflow_scope: str | None = None,
    capabilities: MaintenanceActionCapabilities | None = None,
    evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the redacted persisted progress contract used by the maintenance UI."""

    ordered_steps = tuple(sorted(steps, key=lambda item: item.sequence))
    ordered_checkpoints = tuple(sorted(checkpoints, key=lambda item: item.sequence))
    latest = ordered_checkpoints[-1] if ordered_checkpoints else None
    evidence = redact_structure(evidence or {})
    cleanup = list(evidence.get("cleanup", ())) if isinstance(evidence, Mapping) else []
    safe, safe_reason = _safe_checkpoint(latest)
    completed = sum(item.state in {MaintenanceStepState.VERIFIED, MaintenanceStepState.SKIPPED} for item in ordered_steps)
    active_step = next((item for item in ordered_steps if item.state in {MaintenanceStepState.EXECUTING, MaintenanceStepState.RECOVERY_REQUIRED}), None)
    last_verified = next((item for item in reversed(ordered_steps) if item.state == MaintenanceStepState.VERIFIED), None)
    active_checkpoint = None
    if latest is not None:
        checkpoint_state = "recovery_required" if plan.lifecycle_state == MaintenanceState.RECOVERY_REQUIRED else "verified" if latest.side_effect_state == SideEffectState.VERIFIED else "active" if latest.side_effect_state == SideEffectState.MAY_HAVE_STARTED else "pending"
        active_checkpoint = {"id": latest.checkpoint_key, "label": _checkpoint_label(latest.checkpoint_key), "state": checkpoint_state, "safeForOperatorAction": safe, "detail": safe_reason, "updatedAt": latest.classified_at or latest.created_at}
    elif active_step is not None:
        active_checkpoint = {"id": active_step.step_key, "label": _checkpoint_label(active_step.step_kind), "state": "recovery_required" if active_step.state == MaintenanceStepState.RECOVERY_REQUIRED else "active", "safeForOperatorAction": safe, "detail": safe_reason, "updatedAt": active_step.updated_at}
    progress = {
        "lifecycleState": plan.lifecycle_state.value, "progress": {"completed": completed, "total": len(ordered_steps)},
        "workflowState": workflow_state.value if workflow_state is not None else None,
        "workflowScope": workflow_scope,
        "activeCheckpoint": active_checkpoint,
        "lastVerifiedCheckpoint": ({"label": _checkpoint_label(last_verified.step_kind), "verifiedAt": last_verified.finished_at} if last_verified else None),
        "hostBoot": _host_boot(ordered_checkpoints, evidence.get("host_boot", {})),
        "cleanup": cleanup, "executor": _executor(ordered_checkpoints, evidence.get("executor", {})),
    }
    if host_state is not None and host_state.state == HostMaintenanceState.RECOVERY_REQUIRED:
        progress["lifecycleState"] = MaintenanceState.RECOVERY_REQUIRED.value
    return redact_structure({"progress": progress, "safe_checkpoint": safe, "safe_checkpoint_reason": safe_reason, "action_controls": _actions(plan.lifecycle_state, capabilities or MaintenanceActionCapabilities(), safe, latest, cleanup)})
