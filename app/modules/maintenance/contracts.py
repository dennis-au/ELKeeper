"""Public provider contracts shared with other ELKeeper modules."""

from .models import MaintenanceBackend, ProviderType
from .provider import OwnershipState, ProviderProfile
from .workload_contracts import (
    DisruptionBudget,
    LegacyBatchRecoveryDecision,
    LegacyWorkloadObservation,
    ReadinessEvidence,
    WorkloadCheckpoint,
    WorkloadMaintenancePlanInput,
    WorkloadMaintenanceTarget,
    WorkloadOperation,
    WorkloadRole,
    classify_legacy_batch_recovery,
    legacy_role_to_workload_role,
    rollback_allowed,
    validate_readiness,
)

__all__ = [
    "DisruptionBudget",
    "LegacyBatchRecoveryDecision",
    "LegacyWorkloadObservation",
    "MaintenanceBackend",
    "OwnershipState",
    "ProviderProfile",
    "ReadinessEvidence",
    "WorkloadCheckpoint",
    "WorkloadMaintenancePlanInput",
    "WorkloadMaintenanceTarget",
    "WorkloadOperation",
    "WorkloadRole",
    "classify_legacy_batch_recovery",
    "legacy_role_to_workload_role",
    "rollback_allowed",
    "validate_readiness",
]
