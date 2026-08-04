import { api, jsonBody } from '../../shared/api';
import type { StorageMountResponse } from '../hosts';
import type { TopologyResponse, WorkloadApplyResult, WorkloadChangeSet, WorkloadRole, WorkloadRunResult } from './types';

/** Workload API client for role assignment lifecycle and storage discovery. */
export const workloadsApi = {
  storage: (nodeId: number) => api<StorageMountResponse>(`/api/nodes/${nodeId}/storage`),

  removeAssignment: (assignmentId: number, mode: 'detach' | 'purge') =>
    api<WorkloadRunResult>(`/api/assignments/${assignmentId}?mode=${mode}`, { method: 'DELETE' }),

  applyChanges: (clusterId: number, payload: WorkloadChangeSet) =>
    api<WorkloadApplyResult>(`/api/clusters/${clusterId}/workload-changes/apply`, { method: 'POST', ...jsonBody(payload) }),

  topology: (clusterId: number) => api<TopologyResponse>(`/api/clusters/${clusterId}/topology`),

  roles: () => api<{ roles: WorkloadRole[] }>('/api/health'),
};
