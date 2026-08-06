/** Public maintenance capability and policy contracts. */
export interface MaintenanceCapabilities {
  planning: boolean;
  operations: {
    manual_maintenance_entry?: boolean;
    container_stop?: boolean;
    host_shutdown?: boolean;
    host_reboot: boolean;
    rolling_restart: boolean;
    upgrade: boolean;
    evacuation: boolean;
  };
  lifecycle?: {
    manual_maintenance_exit: boolean;
    recovery: boolean;
  };
  backends: {
    documented_rolling: boolean;
    node_shutdown: boolean;
  };
}

export interface MaintenancePolicy {
  max_unavailable: number;
  max_surge: 0;
  minimum_master_eligible: number | 'quorum';
  minimum_data_per_tier: number;
  minimum_kibana: number;
  minimum_fleet_server: number;
  minimum_logstash: number;
  minimum_coordinating: number;
  allow_agent_interruption: 'true-with-warning' | 'block';
  required_cluster_health: 'green' | 'yellow';
  allocation_guard: 'primaries-for-data' | 'none';
  observation_max_age_seconds: number;
  restart_allocation_delay_seconds: number | null;
  host_return_timeout_seconds: number;
  workload_ready_timeout_seconds: number;
  plan_validity_seconds: number;
}

export interface MaintenancePolicyResponse {
  policy: MaintenancePolicy;
  revision: number;
  customized: boolean;
  updated_by?: string | null;
  updated_at?: string | null;
}

export type MaintenancePreviewOperation =
  | 'reboot'
  | 'manual_maintenance'
  | 'host_maintenance'
  | 'container_maintenance'
  | 'resource_change'
  | 'cluster_settings'
  | 'zoning'
  | 'apply'
  | 'detach'
  | 'purge'
  | 'download'
  | 'upgrade';

export interface MaintenancePlanListFilters {
  node_id?: number;
  host_id?: number;
  cluster_id?: number;
  state?: string;
  limit?: number;
}

export interface ManualMaintenanceMode {
  node_id: number;
  state: 'available' | 'planning' | 'maintenance' | 'recovery_required';
  state_revision: number;
  workflow_state?: 'available' | 'preparing' | 'ready_to_stop' | 'stopping' | 'maintenance' | 'returning' | 'verifying' | 'blocked' | 'recovery_required';
  workflow_state_revision?: number;
  plan_id: string | null;
  run_id: number | null;
  expires_at: string | null;
  lifecycle_state: string | null;
}

export interface MaintenancePlanHistoryItem {
  plan_id: string;
  lifecycle_state: string;
  view: {
    header: {
      planId: string;
      state: string;
      target: { kind: string; id?: string | number; name: string };
      operation: string;
      reason: string;
      requester: string;
      createdAt: string;
      freshness: { state: string; observedAt?: string; expiresAt?: string; detail?: string };
      policy: { name: string; revision: number; availabilityMode: string };
    };
    [key: string]: unknown;
  };
}

export interface MaintenancePlanHistoryResponse {
  items: MaintenancePlanHistoryItem[];
  count: number;
}
