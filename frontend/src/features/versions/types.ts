import type { Assignment, WorkloadObservation } from '../workloads';

export type { Assignment, WorkloadObservation };

export interface VersionResponse {
  assignments: Array<Assignment & { desired_version: string }>;
  available_versions: string[];
  recommended_version?: string;
  registry_error?: string;
}

export interface VersionActionResult {
  run_id: number;
}
