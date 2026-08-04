import { api, jsonBody } from '../../shared/api';
import type {
  HostAction,
  HostMaintenancePlanInput,
  HostPasswordTestInput,
  HostPasswordTestResult,
  HostRunRequiredResult,
  HostRunResult,
  HostSaveResult,
  HostZoneUpdate,
} from './types';
import type { NodeRecord } from './types';

/** Host API client. URL construction and mutation verbs live here; page components own presentation. */
export const hostApi = {
  list: () => api<NodeRecord[]>('/api/nodes'),
  testPassword: (input: HostPasswordTestInput) =>
    api<HostPasswordTestResult>('/api/nodes/test-password', { method: 'POST', ...jsonBody(input) }),

  save: <T extends HostSaveResult = HostSaveResult>(nodeId: number | undefined, input: unknown) =>
    api<T>(nodeId ? `/api/nodes/${nodeId}` : '/api/nodes/enroll', {
      method: nodeId ? 'PUT' : 'POST',
      ...jsonBody(input),
    }),

  removeLegacyKnownHosts: (nodeId: number) =>
    api<void>(`/api/nodes/${nodeId}/legacy-known-hosts/remove`, { method: 'POST' }),

  remove: (nodeId: number, query = '') =>
    api<HostRunResult>(`/api/nodes/${nodeId}${query ? `?${query}` : ''}`, { method: 'DELETE' }),

  action: (nodeId: number, action: HostAction) =>
    api<HostRunRequiredResult>(`/api/nodes/${nodeId}/${action}`, { method: 'POST' }),

  probe: (nodeId: number) =>
    api<HostRunRequiredResult>(`/api/nodes/${nodeId}/probe`, { method: 'POST' }),

  installControllerKey: (nodeId: number, password: string) =>
    api<HostRunRequiredResult>(`/api/nodes/${nodeId}/controller-key`, {
      method: 'POST',
      ...jsonBody({ password }),
    }),

  updateZone: (nodeId: number, input: HostZoneUpdate) =>
    api<HostRunRequiredResult>(`/api/nodes/${nodeId}/zone`, { method: 'PUT', ...jsonBody(input) }),

  getMaintenancePlan: <T>(planId: string) =>
    api<T>(`/api/maintenance/plans/${planId}`),

  createMaintenancePlan: <T>(nodeId: number, input: HostMaintenancePlanInput) =>
    api<T>(`/api/nodes/${nodeId}/maintenance/plans`, { method: 'POST', ...jsonBody(input) }),

  maintenanceAction: (planId: string, action: string) =>
    api<HostRunResult>(`/api/maintenance/plans/${planId}/${action}`, { method: 'POST' }),
};
