import type { Cluster, ControllerSettings, DashboardSnapshot, MaintenanceCapabilities, NodeRecord, RunRecord } from './types';

const TOKEN_KEY = 'elastic-control-token';

export function getToken() {
  return sessionStorage.getItem(TOKEN_KEY) || '';
}

export function setToken(token: string) {
  if (token) sessionStorage.setItem(TOKEN_KEY, token);
  else sessionStorage.removeItem(TOKEN_KEY);
  window.dispatchEvent(new Event('elastic-auth-change'));
}

interface ApiOptions extends RequestInit {
  suppressAuthExpiry?: boolean;
}

export async function api<T>(path: string, options: ApiOptions = {}): Promise<T> {
  const { suppressAuthExpiry = false, ...requestOptions } = options;
  const headers = new Headers(requestOptions.headers);
  const token = getToken();
  if (token) headers.set('Authorization', `Bearer ${token}`);
  if (requestOptions.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
  const response = await fetch(path, { ...requestOptions, headers });
  if (response.status === 204) return undefined as T;
  const payload = await response.json().catch(() => ({ detail: 'Unexpected server response' }));
  if (!response.ok) {
    if (response.status === 401 && !suppressAuthExpiry) window.dispatchEvent(new Event('elastic-auth-expired'));
    throw new Error(payload.detail || 'Request failed');
  }
  return payload as T;
}

export const queries = {
  clusters: () => api<Cluster[]>('/api/clusters'),
  nodes: () => api<NodeRecord[]>('/api/nodes'),
  runs: () => api<RunRecord[]>('/api/runs'),
  dashboard: () => api<DashboardSnapshot>('/api/dashboard/snapshot'),
  controllerSettings: () => api<ControllerSettings>('/api/controller/settings'),
  maintenanceCapabilities: () => api<MaintenanceCapabilities>('/api/maintenance/capabilities'),
};

export function jsonBody(value: unknown): RequestInit {
  return { body: JSON.stringify(value), headers: { 'Content-Type': 'application/json' } };
}
