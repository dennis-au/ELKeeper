from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Protocol, Sequence, TYPE_CHECKING

from app.modules.maintenance.lifecycle import SideEffectState

if TYPE_CHECKING:
    from .store import CheckpointRecord, PlanRecord


class RecoveryClassification(str, Enum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    AMBIGUOUS = "ambiguous"
    SAFE_TO_RESUME = "safe_to_resume"


class StartupRecoveryClassification(str, Enum):
    """Classification persisted by the read-only startup recovery pass.

    ``SAFE_TO_RESUME`` is retained by :func:`classify_recovery` for the
    existing checkpoint API.  Startup recovery deliberately exposes the
    operator-facing vocabulary required by the maintenance lifecycle and
    maps that legacy value to ``INCOMPLETE`` with ``resumable=True``.
    """

    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    AMBIGUOUS = "ambiguous"
    RECOVERY_REQUIRED = "recovery_required"


@dataclass(frozen=True)
class RecoveryEvidence:
    side_effect_state: SideEffectState | str
    observation_complete: bool
    observed_fingerprint: str | None = None
    before_fingerprint: str | None = None
    after_fingerprint: str | None = None
    identity_matches: bool = True
    resume_is_idempotent: bool = False


@dataclass(frozen=True)
class RecoveryDecision:
    classification: RecoveryClassification
    reason_code: str
    resumable: bool


@dataclass(frozen=True)
class RecoveryProjectionResult:
    """Redacted observation returned by one named recovery projection."""

    source: str
    complete: bool
    observed_fingerprint: str | None = None
    before_fingerprint: str | None = None
    after_fingerprint: str | None = None
    identity_matches: bool = True
    resume_is_idempotent: bool = False
    reason_code: str | None = None


class HostRecoveryProjection(Protocol):
    """Public host-state projection used during startup only."""

    source: str

    def observe(self, plan: "PlanRecord", checkpoint: "CheckpointRecord") -> RecoveryProjectionResult:
        ...


class WorkloadRecoveryProjection(Protocol):
    """Public workload-state projection used during startup only."""

    source: str

    def observe(self, plan: "PlanRecord", checkpoint: "CheckpointRecord") -> RecoveryProjectionResult:
        ...


class ObservabilityRecoveryProjection(Protocol):
    """Public telemetry/observation projection used during startup only."""

    source: str

    def observe(self, plan: "PlanRecord", checkpoint: "CheckpointRecord") -> RecoveryProjectionResult:
        ...


class ElasticsearchRecoveryProjection(Protocol):
    """Public CA-verified Elasticsearch state projection used during startup."""

    source: str

    def observe(self, plan: "PlanRecord", checkpoint: "CheckpointRecord") -> RecoveryProjectionResult:
        ...


@dataclass(frozen=True)
class RecoveryProjectionBundle:
    """The four explicit read-only boundaries required for startup recovery."""

    host: HostRecoveryProjection
    workload: WorkloadRecoveryProjection
    observability: ObservabilityRecoveryProjection
    elasticsearch: ElasticsearchRecoveryProjection


@dataclass(frozen=True)
class StartupRecoveryResult:
    plan_id: str
    checkpoint_id: int | None
    classification: StartupRecoveryClassification
    reason_code: str
    resumable: bool
    sources: tuple[str, ...]


class PersistedCheckpointProjection:
    """Safe default projection over checkpoint-owned redacted observations.

    Startup recovery never performs remote I/O.  Until live adapters are
    injected, each named domain reads only its own observation section from
    the checkpoint journal.  A legacy checkpoint with root-level evidence is
    accepted as a compatibility fallback; missing sections remain ambiguous.
    """

    def __init__(self, source: str):
        self.source = source

    def observe(self, plan: "PlanRecord", checkpoint: "CheckpointRecord") -> RecoveryProjectionResult:
        del plan
        evidence = checkpoint.recovery_evidence
        section: Mapping[str, Any] = {}
        if isinstance(evidence, Mapping):
            candidate = evidence.get(self.source)
            if isinstance(candidate, Mapping):
                section = candidate
            elif any(key in evidence for key in (
                "observed_fingerprint", "before_fingerprint", "after_fingerprint",
                "identity_matches", "observation_complete",
            )):
                section = evidence
        observation = checkpoint.observation if isinstance(checkpoint.observation, Mapping) else {}
        observed = section.get("observed_fingerprint", section.get("fingerprint"))
        if observed is None:
            observed = observation.get(f"{self.source}_fingerprint")
        before = section.get("before_fingerprint")
        after = section.get("after_fingerprint")
        identity = section.get("identity_matches", True)
        complete = section.get("observation_complete", section.get("complete", bool(observed)))
        return RecoveryProjectionResult(
            source=self.source,
            complete=bool(complete),
            observed_fingerprint=str(observed) if observed else None,
            before_fingerprint=str(before) if before else None,
            after_fingerprint=str(after) if after else None,
            identity_matches=bool(identity),
            resume_is_idempotent=bool(section.get("resume_is_idempotent", False)),
            reason_code=str(section.get("reason_code")) if section.get("reason_code") else None,
        )


def default_recovery_projections() -> RecoveryProjectionBundle:
    """Build non-mutating persisted projections for controller bootstrap."""

    return RecoveryProjectionBundle(
        host=PersistedCheckpointProjection("host"),
        workload=PersistedCheckpointProjection("workload"),
        observability=PersistedCheckpointProjection("observability"),
        elasticsearch=PersistedCheckpointProjection("elasticsearch"),
    )


class MaintenanceStartupRecoveryCoordinator:
    """Classify interrupted checkpoints before transient-artifact cleanup.

    The coordinator is intentionally synchronous and side-effect free apart
    from the injected maintenance repository's checkpoint classification
    write.  It does not issue SSH, Ansible, Podman, or Elasticsearch calls.
    """

    def __init__(self, repository: Any, projections: RecoveryProjectionBundle | None = None):
        self.repository = repository
        self.projections = projections or default_recovery_projections()

    def classify_checkpoint(self, plan: "PlanRecord", checkpoint: "CheckpointRecord") -> StartupRecoveryResult:
        projection_results = tuple(
            projection.observe(plan, checkpoint)
            for projection in (
                self.projections.host,
                self.projections.workload,
                self.projections.observability,
                self.projections.elasticsearch,
            )
        )
        sources = tuple(item.source for item in projection_results)
        if any(not item.identity_matches for item in projection_results):
            decision = RecoveryDecision(RecoveryClassification.AMBIGUOUS, "projection_identity_mismatch", False)
        elif any(not item.complete for item in projection_results):
            decision = RecoveryDecision(RecoveryClassification.AMBIGUOUS, "projection_incomplete", False)
        else:
            fingerprints = [item for item in projection_results if item.observed_fingerprint]
            observed = _aggregate_fingerprint(fingerprints, "observed_fingerprint")
            before = _aggregate_fingerprint(fingerprints, "before_fingerprint")
            after = _aggregate_fingerprint(fingerprints, "after_fingerprint")
            idempotent = all(item.resume_is_idempotent for item in projection_results)
            evidence = RecoveryEvidence(
                side_effect_state=checkpoint.side_effect_state,
                observation_complete=True,
                observed_fingerprint=observed,
                before_fingerprint=before,
                after_fingerprint=after,
                identity_matches=True,
                resume_is_idempotent=idempotent,
            )
            decision = classify_recovery(evidence)
        classification, resumable = _startup_classification(decision)
        reason = decision.reason_code
        if checkpoint.recovery_evidence.get("startup_reason_code") != reason:
            self.repository.persist_startup_classification(
                checkpoint.id,
                expected_revision=checkpoint.classification_revision,
                classification=classification.value,
                reason_code=reason,
                resumable=resumable,
                evidence={
                    "sources": sources,
                    "classification": classification.value,
                    "reason_code": reason,
                    "resumable": resumable,
                },
            )
        return StartupRecoveryResult(plan.id, checkpoint.id, classification, reason, resumable, sources)

    def classify_plan(self, plan: "PlanRecord") -> StartupRecoveryResult:
        checkpoint = self.repository.latest_checkpoint(plan.id)
        if checkpoint is None:
            return StartupRecoveryResult(
                plan.id, None, StartupRecoveryClassification.AMBIGUOUS,
                "checkpoint_missing", False, (),
            )
        return self.classify_checkpoint(plan, checkpoint)

    def classify_plans(self, plans: Sequence["PlanRecord"]) -> tuple[StartupRecoveryResult, ...]:
        return tuple(self.classify_plan(plan) for plan in plans)


def _aggregate_fingerprint(results: Sequence[RecoveryProjectionResult], field: str) -> str | None:
    values = {str(getattr(item, field)) for item in results if getattr(item, field)}
    if len(values) == 1:
        return next(iter(values))
    return None


def _startup_classification(decision: RecoveryDecision) -> tuple[StartupRecoveryClassification, bool]:
    if decision.classification == RecoveryClassification.COMPLETE:
        return StartupRecoveryClassification.COMPLETE, False
    if decision.classification == RecoveryClassification.SAFE_TO_RESUME:
        return StartupRecoveryClassification.INCOMPLETE, True
    if decision.classification == RecoveryClassification.INCOMPLETE:
        return StartupRecoveryClassification.RECOVERY_REQUIRED, False
    return StartupRecoveryClassification.AMBIGUOUS, False


def classify_recovery(evidence: RecoveryEvidence) -> RecoveryDecision:
    effect_state = SideEffectState(evidence.side_effect_state)
    if not evidence.observation_complete:
        return RecoveryDecision(
            RecoveryClassification.AMBIGUOUS,
            "observation_incomplete",
            False,
        )
    if not evidence.identity_matches:
        return RecoveryDecision(
            RecoveryClassification.AMBIGUOUS,
            "identity_mismatch",
            False,
        )
    if not evidence.observed_fingerprint:
        return RecoveryDecision(
            RecoveryClassification.AMBIGUOUS,
            "observation_fingerprint_missing",
            False,
        )
    if evidence.after_fingerprint and evidence.observed_fingerprint == evidence.after_fingerprint:
        return RecoveryDecision(
            RecoveryClassification.COMPLETE,
            "after_state_observed",
            False,
        )
    if evidence.before_fingerprint and evidence.observed_fingerprint == evidence.before_fingerprint:
        if effect_state in {SideEffectState.NOT_STARTED, SideEffectState.PREPARED}:
            return RecoveryDecision(
                RecoveryClassification.SAFE_TO_RESUME,
                "side_effect_not_started",
                True,
            )
        if effect_state == SideEffectState.MAY_HAVE_STARTED and evidence.resume_is_idempotent:
            return RecoveryDecision(
                RecoveryClassification.SAFE_TO_RESUME,
                "idempotent_side_effect_not_observed",
                True,
            )
        if effect_state == SideEffectState.MAY_HAVE_STARTED:
            return RecoveryDecision(
                RecoveryClassification.INCOMPLETE,
                "side_effect_not_observed",
                False,
            )
        return RecoveryDecision(
            RecoveryClassification.AMBIGUOUS,
            "verified_checkpoint_disagrees_with_observation",
            False,
        )
    return RecoveryDecision(
        RecoveryClassification.AMBIGUOUS,
        "observation_matches_neither_checkpoint_state",
        False,
    )
