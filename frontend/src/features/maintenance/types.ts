/** Public maintenance capability and policy contracts. */
export interface MaintenanceCapabilities {
  planning: boolean;
  operations: {
    host_reboot: boolean;
    rolling_restart: boolean;
    upgrade: boolean;
    evacuation: boolean;
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
