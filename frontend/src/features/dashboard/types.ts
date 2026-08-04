import type { ControllerSettings } from '../advanced';
import type { LogMonitoring } from '../clusters';
import type { ContainerMetric, CrossClusterHostUsage, HostResourceSample, HostRuntime } from '../hosts';
import type { TopologyResponse } from '../workloads';

export type { ControllerSettings, LogMonitoring, ContainerMetric, CrossClusterHostUsage, HostResourceSample, HostRuntime, TopologyResponse };

export type Health = 'green' | 'yellow' | 'red' | 'unknown' | 'awaiting_data';

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
  cross_cluster_host_usage?: CrossClusterHostUsage[];
}

export interface DashboardStreamToken {
  token: string;
}
