import { describe, expect, it, vi } from 'vitest';
import { versionsApi } from './api';

vi.mock('../../shared/api', () => ({
  api: vi.fn(async (path: string, options?: RequestInit) => ({ path, options })),
  jsonBody: (value: unknown) => ({ body: JSON.stringify(value), headers: { 'Content-Type': 'application/json' } }),
}));

describe('versions API contract', () => {
  it('encodes role-scoped discovery without changing the cluster endpoint', async () => {
    const result = await versionsApi.list(7, 'data_hot');
    expect(result).toMatchObject({ path: '/api/clusters/7/versions?role=data_hot' });
  });

  it('keeps download-only and upgrade actions distinct', async () => {
    const download = await versionsApi.download(7, '8.20.0');
    expect(download).toMatchObject({ path: '/api/clusters/7/versions/download', options: { method: 'POST' } });

    const upgrade = await versionsApi.upgrade(7, '8.20.0');
    expect(upgrade).toMatchObject({ path: '/api/clusters/7/upgrades', options: { method: 'POST' } });
    expect((upgrade as unknown as { options: RequestInit }).options.body).toContain('8.20.0');
  });
});
