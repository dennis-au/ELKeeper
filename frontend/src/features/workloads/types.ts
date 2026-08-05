/** Public workload lifecycle and topology contracts. */
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

export interface WorkloadMaintenanceProgress {
  plan_id: string;
  operation: string;
  lifecycle_state: string;
  execution_enabled: false;
  execution_blockers: string[];
  step_count: number;
  verified_steps: number;
  checkpoint?: {
    recovery_classification?: string | null;
    recovery_reason?: string | null;
  } | null;
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
  maintenance?: WorkloadMaintenanceProgress;
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

export interface WorkloadRunResult {
  run_id?: number;
}

export interface WorkloadApplyResult {
  run_id: number;
}

export interface WorkloadChangeSet {
  changes: unknown[];
}

export interface WorkloadRole {
  id: string;
  label: string;
}
