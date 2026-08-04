import { api, jsonBody } from '../../shared/api';
import type { Cluster, ElasticsearchSettings, LogMonitoring } from './types';
import type { VersionResponse } from '../versions';

export interface MembershipInput {
  node_id: number;
  network_mode: 'shared' | 'dedicated';
  data_interface: string;
  data_address: string;
  user_interface: string;
  user_address: string;
}

export const clusterApi = {
  list: () => api<Cluster[]>('/api/clusters'),
  save: (clusterId: number | undefined, payload: unknown) => api<{ id?: number }>(clusterId ? `/api/clusters/${clusterId}` : '/api/clusters', { method: clusterId ? 'PUT' : 'POST', ...jsonBody(payload) }),
  applyZoning: (clusterId: number) => api<{ run_id: number }>(`/api/clusters/${clusterId}/zoning/apply`, { method: 'POST' }),
  updateSettings: (clusterId: number, settings: ElasticsearchSettings) => api<{ run_id: number }>(`/api/clusters/${clusterId}/settings`, { method: 'PUT', ...jsonBody(settings) }),
  updateLogMonitoring: (clusterId: number, input: Pick<LogMonitoring, 'filebeat_enabled'> | LogMonitoring) => api<{ run_id: number }>(`/api/clusters/${clusterId}/log-monitoring`, { method: 'PUT', ...jsonBody(input) }),
  versions: (clusterId: number) => api<VersionResponse>(`/api/clusters/${clusterId}/versions`),
  versionAction: (clusterId: number, action: 'download' | 'upgrade', target_version: string) => api<{ run_id: number }>(`/api/clusters/${clusterId}/${action === 'download' ? 'versions/download' : 'upgrades'}`, { method: 'POST', ...jsonBody({ target_version }) }),
  addMember: (clusterId: number, input: MembershipInput) => api<void>(`/api/clusters/${clusterId}/members`, { method: 'POST', ...jsonBody(input) }),
  updateMember: (clusterId: number, nodeId: number, input: MembershipInput) => api<void>(`/api/clusters/${clusterId}/members/${nodeId}`, { method: 'PUT', ...jsonBody(input) }),
  removeMember: (clusterId: number, nodeId: number) => api<void>(`/api/clusters/${clusterId}/members/${nodeId}`, { method: 'DELETE' }),
  remove: (clusterId: number) => api<void>(`/api/clusters/${clusterId}`, { method: 'DELETE' }),
};
