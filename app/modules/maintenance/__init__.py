"""Public maintenance persistence contracts."""

from .repository import (
    ClusterLookup,
    ConflictObservation,
    HostLookup,
    MaintenanceRepository,
    RunLookup,
    WorkloadLookup,
)
from .http import build_router
from .execution import AdapterResult, MaintenanceAction, MaintenanceExecutionService
from .executor import SignedHostExecutorManifest, executor_paths
from .elasticsearch import ElasticsearchMaintenanceClient, ElasticsearchClientConfig
from .post_return import CleanupStatus, PostReturnCoordinator
from .reboot import RebootOrchestrator
from .runtime import ControllerManagedHostRuntime, MaintenanceRuntimeFlags
from .controller_io import ControllerMaintenanceIOAdapter
from .composition import Phase2RebootAdapterComposition, Phase2RebootAdapterFactory
from .observation import collect_generic_preview_data, collect_host_reboot_planning_data
from .service import (
    HostRebootPlanRequest,
    HostRebootPlanningData,
    MaintenancePlanningService,
    generic_preview_idempotency_key,
    same_generic_preview_request,
)
from .models import (
    MaintenanceBackend,
    MaintenancePlanPreviewInput,
    PreviewOperation,
    ProviderType,
)
from .provider import OwnershipState, ProviderProfile
from .recovery import (
    MaintenanceStartupRecoveryCoordinator,
    RecoveryProjectionBundle,
    RecoveryProjectionResult,
    StartupRecoveryClassification,
    StartupRecoveryResult,
    default_recovery_projections,
)
from .upgrade_planning import (
    MaintenanceUpgradePlanningService,
    UpgradePlanDispatch,
    UpgradeManifestProjection,
    UpgradePlanPreview,
    attach_manifest,
    build_manifest_for_assignments,
    build_upgrade_plan_preview,
    manifest_from_target_manifest,
)
from .workload_engine import (
    WORKLOAD_EXECUTION_CAPABILITY,
    WorkloadMaintenancePlanService,
    workload_maintenance_progress_in_connection,
)

__all__ = [
    "ClusterLookup",
    "ConflictObservation",
    "HostLookup",
    "MaintenanceRepository",
    "RunLookup",
    "WorkloadLookup",
    "build_router",
    "AdapterResult",
    "MaintenanceAction",
    "MaintenanceExecutionService",
    "SignedHostExecutorManifest",
    "executor_paths",
    "ElasticsearchMaintenanceClient",
    "ElasticsearchClientConfig",
    "CleanupStatus",
    "PostReturnCoordinator",
    "RebootOrchestrator",
    "MaintenanceRuntimeFlags",
    "ControllerMaintenanceIOAdapter",
    "Phase2RebootAdapterComposition",
    "Phase2RebootAdapterFactory",
    "ControllerManagedHostRuntime",
    "collect_host_reboot_planning_data",
    "collect_generic_preview_data",
    "HostRebootPlanRequest",
    "HostRebootPlanningData",
    "MaintenancePlanningService",
    "generic_preview_idempotency_key",
    "same_generic_preview_request",
    "MaintenanceBackend",
    "MaintenancePlanPreviewInput",
    "PreviewOperation",
    "ProviderType",
    "OwnershipState",
    "ProviderProfile",
    "MaintenanceStartupRecoveryCoordinator",
    "RecoveryProjectionBundle",
    "RecoveryProjectionResult",
    "StartupRecoveryClassification",
    "StartupRecoveryResult",
    "default_recovery_projections",
    "MaintenanceUpgradePlanningService",
    "UpgradePlanDispatch",
    "UpgradeManifestProjection",
    "UpgradePlanPreview",
    "attach_manifest",
    "build_manifest_for_assignments",
    "build_upgrade_plan_preview",
    "manifest_from_target_manifest",
    "WORKLOAD_EXECUTION_CAPABILITY",
    "WorkloadMaintenancePlanService",
    "workload_maintenance_progress_in_connection",
]
