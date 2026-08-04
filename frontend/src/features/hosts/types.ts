/** Public inventory, runtime, and storage contracts owned by the hosts feature. */
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

export interface HostResourceSample {
  observed_at: string;
  cpu_percent: number | null;
  memory_usage_bytes: number;
  memory_total_bytes: number;
  network_rx_bytes_per_second: number | null;
  network_tx_bytes_per_second: number | null;
  disk_read_bytes_per_second: number | null;
  disk_write_bytes_per_second: number | null;
}

export interface CrossClusterHostUsage {
  node_id: number;
  name: string;
  reachable: boolean;
  observed_at?: string;
  last_error: string;
  resource_observation_error: string;
  clusters: Array<{ id: number; name: string; theme_color: string }>;
  history: HostResourceSample[];
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

export interface HostPasswordTestResult {
  authenticated: boolean;
  message: string;
}

export interface HostRunResult {
  run_id?: number;
}

export interface HostRunRequiredResult {
  run_id: number;
}

export interface HostSaveResult {
  id?: number;
  run_id?: number;
}

export type HostAction = 'initialize' | 'reboot' | 'deinitialize';

export interface HostZoneUpdate {
  cluster_id: number;
  zone_id: string;
}

export interface HostPasswordTestInput {
  address: string;
  ssh_user: string;
  ssh_port: number;
  ssh_host_key?: string;
  password: string;
}

export interface HostMaintenancePlanInput {
  operation: 'reboot';
  reason: string;
  availability_mode: 'zero-impact';
  idempotency_key?: string;
}
