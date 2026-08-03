from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .maintenance_lifecycle import SideEffectState


class RecoveryClassification(str, Enum):
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    AMBIGUOUS = "ambiguous"
    SAFE_TO_RESUME = "safe_to_resume"


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
