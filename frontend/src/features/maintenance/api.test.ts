import { describe, expect, it, vi } from 'vitest';
import { maintenanceApi } from './api';

vi.mock('../../shared/api', () => ({
  api: vi.fn(async (path: string, options?: RequestInit) => ({ path, options })),
  jsonBody: (value: unknown) => ({ body: JSON.stringify(value), headers: { 'Content-Type': 'application/json' } }),
}));

describe('maintenance API contract', () => {
  it('creates generic previews without changing the public operation payload', async () => {
    const result = await maintenanceApi.preview({ operation: 'upgrade', cluster_id: 4 });
    expect(result).toMatchObject({ path: '/api/maintenance/plans/preview', options: { method: 'POST' } });
  });

  it('serializes optional plan list filters and preserves host aliases', async () => {
    const result = await maintenanceApi.listPlans({ host_id: 7, state: 'ready', limit: 10 });
    expect(result).toMatchObject({ path: '/api/maintenance/plans?host_id=7&state=ready&limit=10' });
  });

  it('keeps manual mode operations host-scoped', async () => {
    const mode = await maintenanceApi.manualMode(7);
    const enter = await maintenanceApi.enterManualMode(7, { reason: 'inspection' });
    const exit = await maintenanceApi.exitManualMode(7);
    expect(mode).toMatchObject({ path: '/api/nodes/7/maintenance-mode' });
    expect(enter).toMatchObject({ path: '/api/nodes/7/maintenance-mode/enter', options: { method: 'POST' } });
    expect(exit).toMatchObject({ path: '/api/nodes/7/maintenance-mode/exit', options: { method: 'POST' } });
  });
});
