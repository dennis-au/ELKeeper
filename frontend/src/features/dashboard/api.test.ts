import { describe, expect, it, vi } from 'vitest';
import { dashboardApi } from './api';

vi.mock('../../shared/api', () => ({
  api: vi.fn(async (path: string, options?: RequestInit) => ({ path, options })),
}));

describe('dashboard API contract', () => {
  it('keeps dashboard reads and stream token requests scoped to their endpoints', async () => {
    const snapshot = await dashboardApi.snapshot();
    expect(snapshot).toMatchObject({ path: '/api/dashboard/snapshot' });

    const settings = await dashboardApi.controllerSettings();
    expect(settings).toMatchObject({ path: '/api/controller/settings' });

    const token = await dashboardApi.streamToken();
    expect(token).toMatchObject({ path: '/api/dashboard/stream-token', options: { method: 'POST' } });
  });

  it('keeps topology requests cluster-scoped', async () => {
    const topology = await dashboardApi.topology(12);
    expect(topology).toMatchObject({ path: '/api/clusters/12/topology' });
  });
});
