import { describe, expect, it, vi } from 'vitest';
import { clusterApi } from './api';

vi.mock('../../shared/api', () => ({
  api: vi.fn(async (path: string, options?: RequestInit) => ({ path, options })),
  jsonBody: (value: unknown) => ({ body: JSON.stringify(value), headers: { 'Content-Type': 'application/json' } }),
}));

describe('cluster API contract', () => {
  it('keeps version actions cluster-scoped', async () => {
    const result = await clusterApi.versionAction(7, 'download', '8.19.0');
    expect(result).toMatchObject({ path: '/api/clusters/7/versions/download' });
  });
});
