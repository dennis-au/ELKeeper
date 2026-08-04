import { describe, expect, it, vi } from 'vitest';
import { workloadsApi } from './api';

vi.mock('../../shared/api', () => ({
  api: vi.fn(async (path: string, options?: RequestInit) => ({ path, options })),
  jsonBody: (value: unknown) => ({ body: JSON.stringify(value), headers: { 'Content-Type': 'application/json' } }),
}));

describe('workloads API contract', () => {
  it('keeps storage discovery host-scoped', async () => {
    const result = await workloadsApi.storage(5);
    expect(result).toMatchObject({ path: '/api/nodes/5/storage' });
  });

  it('keeps detach and purge assignment-scoped', async () => {
    const detach = await workloadsApi.removeAssignment(9, 'detach');
    expect(detach).toMatchObject({ path: '/api/assignments/9?mode=detach', options: { method: 'DELETE' } });

    const purge = await workloadsApi.removeAssignment(9, 'purge');
    expect(purge).toMatchObject({ path: '/api/assignments/9?mode=purge', options: { method: 'DELETE' } });
  });

  it('posts a cluster-scoped change set', async () => {
    const result = await workloadsApi.applyChanges(4, { changes: [{ kind: 'create' }] });
    expect(result).toMatchObject({ path: '/api/clusters/4/workload-changes/apply', options: { method: 'POST' } });
  });
});
