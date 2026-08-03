from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
import json
from typing import Any

from pydantic import BaseModel

from .maintenance_models import (
    CompiledPlan,
    ExecutionValidation,
    ImpactManifest,
    MaintenanceBackend,
    MaintenancePolicy,
    ObservationSnapshot,
    PlanStep,
    PlanningTarget,
    PredicateOutcome,
    PredicateResult,
    RevisionObservation,
    RollbackBoundary,
    SourceStatus,
)


def _canonical_value(value: Any):
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="python")
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Canonical timestamps must include a timezone")
        normalized = value.astimezone(timezone.utc)
        timespec = "microseconds" if normalized.microsecond else "seconds"
        return normalized.isoformat(timespec=timespec).replace("+00:00", "Z")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _canonical_value(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"Unsupported canonical value type: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(
        _canonical_value(value),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _plan_payload(plan: CompiledPlan | dict) -> dict:
    if isinstance(plan, CompiledPlan):
        payload = plan.model_dump(mode="python")
    else:
        payload = dict(plan)
    payload.pop("plan_hash", None)
    return payload


def compile_plan(
    *,
    target: PlanningTarget,
    policy: MaintenancePolicy,
    policy_revision: int,
    backend: MaintenanceBackend,
    observation: ObservationSnapshot,
    predicates: tuple[PredicateResult, ...],
    impact: ImpactManifest,
    steps: tuple[PlanStep, ...],
    rollback_boundaries: tuple[RollbackBoundary, ...],
    created_at: datetime,
    idempotency_key: str | None = None,
) -> CompiledPlan:
    if policy_revision < 0:
        raise ValueError("policy_revision cannot be negative")
    if created_at.tzinfo is None or created_at.utcoffset() is None:
        raise ValueError("created_at must include a timezone")
    ordered_steps = tuple(sorted(steps, key=lambda item: item.sequence))
    if not ordered_steps:
        raise ValueError("A compiled plan requires at least one step")
    expected_sequences = tuple(range(1, len(ordered_steps) + 1))
    if tuple(item.sequence for item in ordered_steps) != expected_sequences:
        raise ValueError("Plan step sequences must be contiguous and start at one")
    predicate_ids = [item.identifier for item in predicates]
    if len(predicate_ids) != len(set(predicate_ids)):
        raise ValueError("A plan cannot contain duplicate predicate identifiers")
    step_numbers = set(expected_sequences)
    if any(item.before_step not in step_numbers for item in rollback_boundaries):
        raise ValueError("Rollback boundaries must reference an existing step")
    expires_at = created_at + timedelta(seconds=policy.plan_validity_seconds)
    payload = {
        "schema_version": 1,
        "idempotency_key": idempotency_key,
        "target": target,
        "policy": policy,
        "policy_revision": policy_revision,
        "backend": backend,
        "observation": observation,
        "predicates": predicates,
        "impact": impact,
        "steps": ordered_steps,
        "rollback_boundaries": tuple(sorted(rollback_boundaries, key=lambda item: item.before_step)),
        "created_at": created_at,
        "expires_at": expires_at,
    }
    return CompiledPlan(**payload, plan_hash=canonical_hash(payload))


def verify_plan_hash(plan: CompiledPlan) -> bool:
    return canonical_hash(_plan_payload(plan)) == plan.plan_hash


def _observation_is_fresh(plan: CompiledPlan, now: datetime) -> bool:
    max_age = plan.policy.observation_max_age_seconds
    required_sources = tuple(item for item in plan.observation.sources if item.required)
    if not required_sources or any(item.status != SourceStatus.OK for item in required_sources):
        return False
    timestamps = (plan.observation.captured_at,) + tuple(item.observed_at for item in required_sources)
    return all(0 <= (now - observed_at).total_seconds() <= max_age for observed_at in timestamps)


def validate_plan_for_execution(
    plan: CompiledPlan,
    *,
    now: datetime,
    expected_plan_hash: str,
    current_policy_revision: int,
    current_capability_revision: str,
    current_assignment_revisions: tuple[RevisionObservation, ...],
) -> ExecutionValidation:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must include a timezone")
    issues = []
    if expected_plan_hash != plan.plan_hash or not verify_plan_hash(plan):
        issues.append("plan_hash_mismatch")
    if now >= plan.expires_at:
        issues.append("plan_expired")
    if current_policy_revision != plan.policy_revision:
        issues.append("policy_revision_changed")
    if current_capability_revision != plan.observation.capability_revision:
        issues.append("capability_revision_changed")
    planned_revisions = {item.assignment_id: item.revision for item in plan.observation.assignment_revisions}
    current_revisions = {item.assignment_id: item.revision for item in current_assignment_revisions}
    if any(current_revisions.get(assignment_id) != revision for assignment_id, revision in planned_revisions.items()):
        issues.append("assignment_revision_changed")
    if not _observation_is_fresh(plan, now):
        issues.append("stale_observation")
    if any(item.outcome == PredicateOutcome.BLOCKED for item in plan.predicates):
        issues.append("blocking_predicate")
    return ExecutionValidation(valid=not issues, issue_codes=tuple(issues))
