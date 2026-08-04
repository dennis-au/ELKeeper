"""Maintenance lifecycle and canonicalization contracts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping


class MaintenanceState(str, Enum):
    DRAFT = "draft"
    READY = "ready"
    BLOCKED = "blocked"
    EXECUTING = "executing"
    PAUSED = "paused"
    RECOVERY_REQUIRED = "recovery_required"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class MaintenanceStepState(str, Enum):
    PENDING = "pending"
    EXECUTING = "executing"
    VERIFIED = "verified"
    SKIPPED = "skipped"
    FAILED = "failed"
    RECOVERY_REQUIRED = "recovery_required"


class HostMaintenanceState(str, Enum):
    AVAILABLE = "available"
    PLANNING = "planning"
    MAINTENANCE = "maintenance"
    DRAINING = "draining"
    RECOVERY_REQUIRED = "recovery_required"


class LockScope(str, Enum):
    HOST = "host"
    CLUSTER = "cluster"
    ASSIGNMENT = "assignment"
    RECOVERY = "recovery"


class SideEffectState(str, Enum):
    NOT_STARTED = "not_started"
    PREPARED = "prepared"
    MAY_HAVE_STARTED = "may_have_started"
    VERIFIED = "verified"


class TransitionError(ValueError):
    pass


PLAN_TRANSITIONS: dict[MaintenanceState, frozenset[MaintenanceState]] = {
    MaintenanceState.DRAFT: frozenset({MaintenanceState.READY, MaintenanceState.BLOCKED, MaintenanceState.CANCELLED}),
    MaintenanceState.READY: frozenset({MaintenanceState.EXECUTING, MaintenanceState.BLOCKED, MaintenanceState.CANCELLED}),
    MaintenanceState.BLOCKED: frozenset({MaintenanceState.DRAFT, MaintenanceState.READY, MaintenanceState.FAILED, MaintenanceState.CANCELLED}),
    MaintenanceState.EXECUTING: frozenset({MaintenanceState.PAUSED, MaintenanceState.RECOVERY_REQUIRED, MaintenanceState.SUCCEEDED, MaintenanceState.FAILED}),
    MaintenanceState.PAUSED: frozenset({MaintenanceState.EXECUTING, MaintenanceState.RECOVERY_REQUIRED, MaintenanceState.FAILED, MaintenanceState.CANCELLED}),
    MaintenanceState.RECOVERY_REQUIRED: frozenset({MaintenanceState.EXECUTING, MaintenanceState.SUCCEEDED, MaintenanceState.FAILED, MaintenanceState.CANCELLED}),
    MaintenanceState.SUCCEEDED: frozenset(),
    MaintenanceState.FAILED: frozenset(),
    MaintenanceState.CANCELLED: frozenset(),
}

STEP_TRANSITIONS: dict[MaintenanceStepState, frozenset[MaintenanceStepState]] = {
    MaintenanceStepState.PENDING: frozenset({MaintenanceStepState.EXECUTING, MaintenanceStepState.SKIPPED, MaintenanceStepState.FAILED}),
    MaintenanceStepState.EXECUTING: frozenset({MaintenanceStepState.VERIFIED, MaintenanceStepState.FAILED, MaintenanceStepState.RECOVERY_REQUIRED}),
    MaintenanceStepState.RECOVERY_REQUIRED: frozenset({MaintenanceStepState.EXECUTING, MaintenanceStepState.VERIFIED, MaintenanceStepState.FAILED, MaintenanceStepState.SKIPPED}),
    MaintenanceStepState.VERIFIED: frozenset(),
    MaintenanceStepState.SKIPPED: frozenset(),
    MaintenanceStepState.FAILED: frozenset(),
}

HOST_TRANSITIONS: dict[HostMaintenanceState, frozenset[HostMaintenanceState]] = {
    HostMaintenanceState.AVAILABLE: frozenset({HostMaintenanceState.PLANNING}),
    HostMaintenanceState.PLANNING: frozenset({HostMaintenanceState.AVAILABLE, HostMaintenanceState.MAINTENANCE, HostMaintenanceState.RECOVERY_REQUIRED}),
    HostMaintenanceState.MAINTENANCE: frozenset({HostMaintenanceState.AVAILABLE, HostMaintenanceState.DRAINING, HostMaintenanceState.RECOVERY_REQUIRED}),
    HostMaintenanceState.DRAINING: frozenset({HostMaintenanceState.AVAILABLE, HostMaintenanceState.MAINTENANCE, HostMaintenanceState.RECOVERY_REQUIRED}),
    HostMaintenanceState.RECOVERY_REQUIRED: frozenset({HostMaintenanceState.AVAILABLE, HostMaintenanceState.PLANNING, HostMaintenanceState.MAINTENANCE}),
}


def validate_transition(current: Enum, target: Enum, transitions: Mapping[Enum, frozenset[Enum]]) -> None:
    if current == target:
        raise TransitionError(f"Repeated transition to {target.value} is not allowed")
    if target not in transitions[current]:
        raise TransitionError(f"Transition from {current.value} to {target.value} is not allowed")


def validate_plan_transition(current: MaintenanceState | str, target: MaintenanceState | str) -> None:
    validate_transition(MaintenanceState(current), MaintenanceState(target), PLAN_TRANSITIONS)


def validate_step_transition(current: MaintenanceStepState | str, target: MaintenanceStepState | str) -> None:
    validate_transition(MaintenanceStepState(current), MaintenanceStepState(target), STEP_TRANSITIONS)


def validate_host_transition(current: HostMaintenanceState | str, target: HostMaintenanceState | str) -> None:
    validate_transition(HostMaintenanceState(current), HostMaintenanceState(target), HOST_TRANSITIONS)


SENSITIVE_KEY_PARTS = ("api_key", "credential", "enrollment", "passphrase", "password", "private_key", "secret", "token")


def _sensitive_key(value: object) -> bool:
    normalized = str(value).strip().lower().replace("-", "_")
    return any(part in normalized for part in SENSITIVE_KEY_PARTS)


def redact_structure(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): "[REDACTED]" if _sensitive_key(key) else redact_structure(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_structure(item) for item in value]
    return value


def _canonical_value(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Canonical timestamps must include a timezone")
        return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _canonical_value(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"Unsupported canonical value type: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(_canonical_value(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PlanHashInput:
    operation_kind: str
    plan: Mapping[str, Any]
    observation: Mapping[str, Any] | None = None
    target_node_id: int | None = None
    target_cluster_id: int | None = None
    target_assignment_id: int | None = None
    expected_policy_revision: int | None = None
    expected_assignment_revision: int | None = None
    observed_at: str | None = None
    target_manifest: Mapping[str, Any] | None = None


def canonical_plan_hash(value: PlanHashInput) -> str:
    return canonical_hash({
        "operation_kind": value.operation_kind,
        "plan": redact_structure(value.plan),
        "observation": redact_structure(value.observation or {}),
        "target_node_id": value.target_node_id,
        "target_cluster_id": value.target_cluster_id,
        "target_assignment_id": value.target_assignment_id,
        "expected_policy_revision": value.expected_policy_revision,
        "expected_assignment_revision": value.expected_assignment_revision,
        "observed_at": value.observed_at,
        "target_manifest": redact_structure(value.target_manifest or {}),
    })


def derive_idempotency_key(operation_kind: str, target_scope: LockScope | str, target_identifier: str | int, request_key: str) -> str:
    if not request_key.strip():
        raise ValueError("request_key must not be blank")
    return canonical_hash({
        "operation_kind": operation_kind,
        "target_scope": LockScope(target_scope).value,
        "target_identifier": str(target_identifier),
        "request_key": request_key,
    })
