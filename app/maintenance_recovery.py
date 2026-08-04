from __future__ import annotations

"""Compatibility facade for the maintenance recovery contract."""

from app.modules.maintenance.recovery import (
    RecoveryClassification,
    RecoveryDecision,
    RecoveryEvidence,
    classify_recovery,
)

__all__ = [
    "RecoveryClassification",
    "RecoveryDecision",
    "RecoveryEvidence",
    "classify_recovery",
]
