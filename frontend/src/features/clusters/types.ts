import type { Assignment } from '../workloads';

/** Public cluster, membership, and cluster-configuration contracts. */
export interface PortProfile {
  elasticsearch_http: number;
  elasticsearch_transport: number;
  kibana: number;
  fleet: number;
  logstash_api: number;
}

export type RolePortProfile = Record<string, Record<string, number>>;

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

export interface LogMonitoring {
  filebeat_enabled: boolean;
  retention_days: number;
  companion_state?: 'running' | 'degraded' | 'pending' | 'disabled';
}

export interface ElasticsearchSettings {
  allocation_enable: 'all' | 'primaries' | 'new_primaries' | 'none';
  rebalance_enable: 'all' | 'primaries' | 'replicas' | 'none';
  disk_watermark_low: string;
  disk_watermark_high: string;
  disk_watermark_flood_stage: string;
  recovery_max_bytes_per_sec: string;
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
