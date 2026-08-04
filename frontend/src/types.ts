/**
 * Compatibility type facade.
 *
 * Feature modules own their public DTOs. New application code should import from
 * the relevant feature package rather than this legacy aggregate module.
 */
export type {
  AlertRecord,
  ClusterMetrics,
  ClusterSummary,
  DashboardSnapshot,
  Health,
  NodeBreakdown,
  ZoneBreakdown,
} from './features/dashboard';
export type {
  Cluster,
  ElasticsearchSettings,
  LogMonitoring,
  Membership,
  PortProfile,
  RolePortProfile,
  ZoningConfig,
  ZoningStatus,
} from './features/clusters';
export type {
  ContainerMetric,
  CrossClusterHostUsage,
  HostResourceSample,
  HostRuntime,
  NodeRecord,
  StorageMount,
  StorageMountResponse,
} from './features/hosts';
export type {
  MaintenanceCapabilities,
  MaintenancePolicy,
  MaintenancePolicyResponse,
} from './features/maintenance';
export type {
  ControllerSettings,
  ControllerSshKey,
  ControllerSshKeyStatus,
  SensitiveItem,
} from './features/advanced';
export type { RunRecord } from './features/runs';
export type {
  AccessUrl,
  Assignment,
  FilebeatCompanionObservation,
  TopologyResponse,
  WorkloadObservation,
} from './features/workloads';
export type { VersionResponse } from './features/versions';
