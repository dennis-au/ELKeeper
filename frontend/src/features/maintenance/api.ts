import { api, jsonBody } from '../../shared/api';
import type {
  MaintenanceCapabilities,
  MaintenancePlanListFilters,
  MaintenancePolicyResponse,
  ManualMaintenanceMode,
} from './types';

export interface MaintenancePlanResponse { plan: Record<string, unknown> }

export const maintenanceApi = {
  capabilities: () => api<MaintenanceCapabilities>('/api/maintenance/capabilities'),
  policy: (clusterId: number) => api<MaintenancePolicyResponse>(`/api/clusters/${clusterId}/maintenance-policy`),
  updatePolicy: (clusterId: number, input: unknown) => api<MaintenancePolicyResponse>(`/api/clusters/${clusterId}/maintenance-policy`, { method: 'PUT', ...jsonBody(input) }),
  getPlan: (planId: string) => api(`/api/maintenance/plans/${encodeURIComponent(planId)}`),
  preview: <T = Record<string, unknown>>(input: unknown) =>
    api<T>('/api/maintenance/plans/preview', { method: 'POST', ...jsonBody(input) }),
  listPlans: <T = Record<string, unknown>>(filters: MaintenancePlanListFilters = {}) => {
    const query = new URLSearchParams();
    Object.entries(filters).forEach(([key, value]) => {
      if (value !== undefined && value !== null && value !== '') query.set(key, String(value));
    });
    const suffix = query.toString() ? `?${query.toString()}` : '';
    return api<T>(`/api/maintenance/plans${suffix}`);
  },
  createPlan: (nodeId: number, input: unknown) => api(`/api/nodes/${nodeId}/maintenance/plans`, { method: 'POST', ...jsonBody(input) }),
  manualMode: (nodeId: number) => api<ManualMaintenanceMode>(`/api/nodes/${nodeId}/maintenance-mode`),
  enterManualMode: (nodeId: number, input: { reason: string; duration_seconds?: number; idempotency_key?: string }) =>
    api<ManualMaintenanceMode>(`/api/nodes/${nodeId}/maintenance-mode/enter`, { method: 'POST', ...jsonBody(input) }),
  exitManualMode: (nodeId: number, input: { reason?: string } = {}) =>
    api<ManualMaintenanceMode>(`/api/nodes/${nodeId}/maintenance-mode/exit`, { method: 'POST', ...jsonBody(input) }),
  action: (planId: string, action: string) => api<{ run_id?: number }>(`/api/maintenance/plans/${encodeURIComponent(planId)}/${encodeURIComponent(action)}`, { method: 'POST' }),
};
