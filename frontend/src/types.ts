export type Health = 'green' | 'yellow' | 'red' | 'unknown' | 'awaiting_data';

export interface PortProfile {
  elasticsearch_http: number;
  elasticsearch_transport: number;
  kibana: number;
  fleet: number;
  logstash_api: number;
}

export type RolePortProfile = Record<string, Record<string, number>>;

export interface NodeRecord {
  id: number;
  name: string;
  address: string;
  ssh_port: number;
  ssh_user: string;
  enabled: boolean;
  ssh_host_key?: string;
  ssh_auth_state?: 'legacy' | 'pending' | 'controller_key' | 'candidate_ready';
  ssh_key_id?: string;
  candidate_key_id?: string;
  legacy_known_hosts_disabled?: boolean;
  zone_id?: string | null;
}

export interface Membership {
  cluster_id: number;
  node_id: number;
  name: string;
  address: string;
  enabled: boolean;
  network_mode: 'shared' | 'dedicated';
  data_interface: string;
  data_address: string;
  user_interface: string;
  user_address: string;
  network_ready: boolean;
  zone_id?: string | null;
}

export interface ZoningConfig {
  mode: 'disabled' | 'awareness' | 'forced_awareness';
  zones: string[];
}

export interface ZoningStatus {
  applied_mode: ZoningConfig['mode'];
  applied_zones: string[];
  observed_zones: Record<string, string>;
  status: 'disabled' | 'pending' | 'applied' | 'drift' | 'failed';
  last_run_id?: number | null;
  observed_at?: string | null;
  last_error?: string;
}

export interface WorkloadObservation {
  image: string;
  digest: string;
  version: string;
  running: boolean;
  cached: boolean;
  observed_at: string;
  error: string;
}

export interface FilebeatCompanionObservation {
  state: 'running' | 'degraded' | 'pending' | 'disabled';
  observed_at?: string;
  error: string;
}

export interface LogMonitoring {
  filebeat_enabled: boolean;
  retention_days: number;
  companion_state?: 'running' | 'degraded' | 'pending' | 'disabled';
}

export interface Assignment {
  id: number;
  cluster_id: number;
  node_id: number;
  node_name: string;
  role: string;
  state: string;
  revision: number;
  image_version?: string;
  config: Record<string, string>;
  observation?: WorkloadObservation;
  filebeat?: FilebeatCompanionObservation;
}

export interface Cluster {
  id: number;
  name: string;
  slug: string;
  ports: PortProfile;
  role_ports?: RolePortProfile;
  theme_color: string;
  desired_version: string;
  network_defaults: { mode: 'shared' | 'dedicated' };
  zoning?: ZoningConfig;
  zoning_status?: ZoningStatus;
  elasticsearch_settings: ElasticsearchSettings;
  log_monitoring?: LogMonitoring;
  members: Membership[];
  assignments: Assignment[];
}

export interface ElasticsearchSettings {
  allocation_enable: 'all' | 'primaries' | 'new_primaries' | 'none';
  rebalance_enable: 'all' | 'primaries' | 'replicas' | 'none';
  disk_watermark_low: string;
  disk_watermark_high: string;
  disk_watermark_flood_stage: string;
  recovery_max_bytes_per_sec: string;
}

export interface ContainerMetric {
  id: string;
  name: string;
  image: string;
  state: string;
  status: string;
  labels: Record<string, string>;
  cpu_percent?: number;
  memory_usage?: number;
  memory_limit?: number;
  network_rx?: number;
  network_tx?: number;
}

export interface HostRuntime extends NodeRecord {
  initialized: boolean;
  reachable: boolean;
  podman_socket_active: boolean;
  os_name: string;
  podman_version: string;
  observed_at?: string;
  last_error: string;
  containers: ContainerMetric[];
  pods: Array<Record<string, unknown>>;
}

export interface ControllerSshKey {
  key_id: string;
  algorithm: string;
  public_key: string;
  source: 'generated' | 'imported' | 'legacy_mounted';
  state: 'active' | 'candidate' | 'legacy';
  created_at?: string | null;
}

export interface ControllerSshKeyStatus {
  active: ControllerSshKey;
  candidate?: ControllerSshKey | null;
  managed: boolean;
}

export interface ControllerSettings {
  timezone: string;
}

export interface StorageMount {
  mount_point: string;
  source: string;
  filesystem: string;
  size_bytes: number;
  available_bytes: number;
  writable: boolean;
  eligible: boolean;
  unavailable_reason: string;
}

export interface StorageMountResponse {
  node_id: number;
  observed_at: string;
  mounts: StorageMount[];
}

export interface NodeBreakdown {
  id: string;
  name: string;
  node_type: string;
  roles: string[];
  zone: string;
  shards: number;
  disk_total_bytes: number;
  disk_available_bytes: number;
  disk_used_bytes: number;
  heap_used_bytes: number;
  heap_max_bytes: number;
}

export interface ZoneBreakdown {
  zone: string;
  nodes: number;
  shards: number;
  disk_total_bytes: number;
  disk_available_bytes: number;
  disk_used_bytes: number;
  heap_used_bytes: number;
  heap_max_bytes: number;
}

export interface ClusterMetrics {
  cluster_id: number;
  status: Health;
  observed_at?: string;
  last_error?: string;
  nodes?: number;
  data_nodes?: number;
  active_primary_shards?: number;
  active_shards?: number;
  unassigned_shards?: number;
  unassigned_primary_shards?: number;
  indices?: number;
  documents?: number;
  store_bytes?: number;
  disk_total_bytes?: number;
  disk_available_bytes?: number;
  heap_used_bytes?: number;
  heap_max_bytes?: number;
  pending_tasks?: number;
  index_health?: Record<string, { status?: string; active_primary_shards?: number; unassigned_shards?: number }>;
  node_breakdown?: NodeBreakdown[];
  zone_breakdown?: ZoneBreakdown[];
}

export interface ClusterSummary {
  id: number;
  name: string;
  slug: string;
  theme_color: string;
  health: Health;
  node_count: number;
  workload_count: number;
  metrics: ClusterMetrics;
  history: ClusterMetrics[];
  log_monitoring?: LogMonitoring;
}

export interface AlertRecord {
  severity: 'critical' | 'warning' | 'info';
  source: 'host' | 'cluster';
  source_id: number;
  message: string;
}

export interface DashboardSnapshot {
  generated_at: string;
  clusters: ClusterSummary[];
  hosts: HostRuntime[];
  alerts: AlertRecord[];
}

export interface RunRecord {
  id: number;
  kind: string;
  target: string;
  status: string;
  log: string;
  created_at: string;
  finished_at?: string;
  events_token: string;
}

export interface SensitiveItem {
  id: string;
  label: string;
  category: string;
  source: string;
  available: boolean;
  masked_value: string;
  fingerprint?: string;
  expires_at?: string;
  storage_path?: string;
}

export interface AccessUrl {
  assignment_id: number;
  role: string;
  label: string;
  audience: 'browser' | 'api';
  host: string;
  port: number;
  url: string;
}

export interface TopologyResponse {
  topology: string;
  access_urls: AccessUrl[];
}

export interface VersionResponse {
  assignments: Array<Assignment & { desired_version: string }>;
  available_versions: string[];
  recommended_version?: string;
  registry_error?: string;
}
