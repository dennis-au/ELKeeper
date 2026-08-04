import { api, jsonBody } from '../../shared/api';
import type { VersionActionResult, VersionResponse } from './types';

/** Cluster-scoped image version discovery and action client. */
export const versionsApi = {
  list: (clusterId: number, role?: string) => {
    const query = role ? `?role=${encodeURIComponent(role)}` : '';
    return api<VersionResponse>(`/api/clusters/${clusterId}/versions${query}`);
  },

  download: (clusterId: number, targetVersion: string) =>
    api<VersionActionResult>(`/api/clusters/${clusterId}/versions/download`, { method: 'POST', ...jsonBody({ target_version: targetVersion }) }),

  upgrade: (clusterId: number, targetVersion: string) =>
    api<VersionActionResult>(`/api/clusters/${clusterId}/upgrades`, { method: 'POST', ...jsonBody({ target_version: targetVersion }) }),
};
