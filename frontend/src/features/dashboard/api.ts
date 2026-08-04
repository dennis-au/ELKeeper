import { api } from '../../shared/api';
import type {
  DashboardSnapshot,
  DashboardStreamToken,
  TopologyResponse,
} from './types';
import type { ControllerSettings } from '../advanced';

/** Dashboard API client. Stream-token and topology URL construction stay feature-owned. */
export const dashboardApi = {
  snapshot: () => api<DashboardSnapshot>('/api/dashboard/snapshot'),
  controllerSettings: () => api<ControllerSettings>('/api/controller/settings'),
  topology: (clusterId: number) => api<TopologyResponse>(`/api/clusters/${clusterId}/topology`),
  streamToken: () => api<DashboardStreamToken>('/api/dashboard/stream-token', { method: 'POST' }),
};
