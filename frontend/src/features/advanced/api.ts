import { api, jsonBody } from '../../shared/api';
import type { ControllerSettings, SensitiveItem } from './types';

export const advancedApi = {
  sensitiveItems: (clusterId: number) => api<{ items: SensitiveItem[] }>(`/api/clusters/${clusterId}/sensitive-items`),
  revealGrant: (clusterId: number, password: string) => api<{ grant_token: string; expires_in: number }>('/api/auth/reveal-grants', { method: 'POST', ...jsonBody({ cluster_id: clusterId, password }), suppressAuthExpiry: true }),
  reveal: (clusterId: number, itemId: string, grantToken: string, purpose: 'reveal' | 'copy') => api<{ value: string; hide_after: number }>(`/api/clusters/${clusterId}/sensitive-items/${encodeURIComponent(itemId)}/reveal`, { method: 'POST', ...jsonBody({ grant_token: grantToken, purpose }), suppressAuthExpiry: true }),
  controllerSettings: () => api<ControllerSettings>('/api/controller/settings'),
  updateControllerSettings: (timezone: string) => api<ControllerSettings>('/api/controller/settings', { method: 'PUT', ...jsonBody({ timezone }) }),
  controllerKey: () => api('/api/controller/ssh-key'),
  controllerKeyAction: (action: 'generate' | 'import' | 'activate', payload: unknown) => api(`/api/controller/ssh-key/${action}`, { method: 'POST', ...jsonBody(payload) }),
};
