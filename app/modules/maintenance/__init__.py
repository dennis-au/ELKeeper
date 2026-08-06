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
from .elasticsearch import (
    AllocationGuardController,
    BasicAuthCredential,
    ElasticsearchMaintenanceClient,
    ElasticsearchClientConfig,
)
from .allocation_guards import AllocationGuardService, ClusterAllocationGuardRouter
from .container_maintenance import (
    ContainerMaintenanceError,
    ContainerMaintenanceService,
    ControllerManagedWorkloadRuntime,
    ManagedContainerTarget,
    RuntimeActionResult,
    resolve_managed_container_target,
)
from .container_actions import (
    ContainerMaintenanceActionDisabled,
    ContainerMaintenanceActionError,
    ContainerMaintenanceActionResult,
    ContainerMaintenanceActionService,
    ContainerWorkflowAction,
)
from .host_actions import (
    HostMaintenanceActionDisabled,
    HostMaintenanceActionError,
    HostMaintenanceActionResult,
    HostMaintenanceActionService,
    HostWorkflowAction,
)
from .workflow_http import build_container_workflow_router, build_host_workflow_router
from .host_maintenance import (
    ControllerManagedServiceAvailability,
    ControllerManagedHostMaintenanceRuntime,
    HostMaintenanceError,
    HostRebootExecutor,
    HostRebootExecutorFactory,
    HostMaintenanceService,
    resolve_managed_container_targets_for_host,
)
from .host_reboot import (
    HostMaintenanceRebootCoordinator,
    HostMaintenanceRebootError,
    HostMaintenanceRebootPredicates,
)
from .workflow_recovery import (
    MaintenanceWorkflowRecoveryService,
    RebootRecoveryDisposition,
    WorkflowRecoveryResult,
)
from .post_return import CleanupStatus, HostMaintenancePostReturnRequest, HostMaintenancePostReturnResult, PostReturnCoordinator
from .reboot import RebootOrchestrator
from .runtime import ControllerManagedHostRuntime, MaintenanceRuntimeFlags
from .controller_io import (
    ControllerMaintenanceIOAdapter,
    ManagedEndpointProbeTarget,
    PooledRemoteCommandSSHRunner,
)
from .ansible_runtime import RunBoundMaintenanceAnsibleRunner
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
    "BasicAuthCredential",
    "AllocationGuardController",
    "AllocationGuardService",
    "ClusterAllocationGuardRouter",
    "ContainerMaintenanceError",
    "ContainerMaintenanceService",
    "ControllerManagedWorkloadRuntime",
    "ManagedContainerTarget",
    "RuntimeActionResult",
    "resolve_managed_container_target",
    "ContainerMaintenanceActionDisabled",
    "ContainerMaintenanceActionError",
    "ContainerMaintenanceActionResult",
    "ContainerMaintenanceActionService",
    "ContainerWorkflowAction",
    "build_container_workflow_router",
    "HostMaintenanceActionDisabled",
    "HostMaintenanceActionError",
    "HostMaintenanceActionResult",
    "HostMaintenanceActionService",
    "HostWorkflowAction",
    "build_host_workflow_router",
    "HostMaintenanceError",
    "HostRebootExecutor",
    "HostRebootExecutorFactory",
    "HostMaintenanceService",
    "ControllerManagedHostMaintenanceRuntime",
    "ControllerManagedServiceAvailability",
    "resolve_managed_container_targets_for_host",
    "HostMaintenanceRebootCoordinator",
    "HostMaintenanceRebootError",
    "HostMaintenanceRebootPredicates",
    "MaintenanceWorkflowRecoveryService",
    "RebootRecoveryDisposition",
    "WorkflowRecoveryResult",
    "CleanupStatus",
    "PostReturnCoordinator",
    "HostMaintenancePostReturnRequest",
    "HostMaintenancePostReturnResult",
    "RebootOrchestrator",
    "MaintenanceRuntimeFlags",
    "ControllerMaintenanceIOAdapter",
    "ManagedEndpointProbeTarget",
    "PooledRemoteCommandSSHRunner",
    "RunBoundMaintenanceAnsibleRunner",
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
