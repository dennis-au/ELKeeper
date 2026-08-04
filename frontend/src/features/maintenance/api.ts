import { api, jsonBody } from '../../shared/api';
import type { MaintenanceCapabilities, MaintenancePolicyResponse } from './types';

export interface MaintenancePlanResponse { plan: Record<string, unknown> }

export const maintenanceApi = {
  capabilities: () => api<MaintenanceCapabilities>('/api/maintenance/capabilities'),
  policy: (clusterId: number) => api<MaintenancePolicyResponse>(`/api/clusters/${clusterId}/maintenance-policy`),
  updatePolicy: (clusterId: number, input: unknown) => api<MaintenancePolicyResponse>(`/api/clusters/${clusterId}/maintenance-policy`, { method: 'PUT', ...jsonBody(input) }),
  getPlan: (planId: string) => api(`/api/maintenance/plans/${encodeURIComponent(planId)}`),
  createPlan: (nodeId: number, input: unknown) => api(`/api/nodes/${nodeId}/maintenance/plans`, { method: 'POST', ...jsonBody(input) }),
  action: (planId: string, action: string) => api<{ run_id?: number }>(`/api/maintenance/plans/${encodeURIComponent(planId)}/${encodeURIComponent(action)}`, { method: 'POST' }),
};
