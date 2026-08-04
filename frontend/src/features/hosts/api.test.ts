import { describe, expect, it, vi } from 'vitest';
import { hostApi } from './api';

vi.mock('../../shared/api', () => ({
  api: vi.fn(async (path: string, options?: RequestInit) => ({ path, options })),
  jsonBody: (value: unknown) => ({ body: JSON.stringify(value), headers: { 'Content-Type': 'application/json' } }),
}));

describe('host API contract', () => {
  it('keeps enrollment and host mutations under the node resource', async () => {
    const enrolled = await hostApi.save(undefined, { address: '192.0.2.10' });
    expect(enrolled).toMatchObject({ path: '/api/nodes/enroll', options: { method: 'POST' } });

    const updated = await hostApi.save(3, { name: 'node-3' });
    expect(updated).toMatchObject({ path: '/api/nodes/3', options: { method: 'PUT' } });
  });

  it('preserves run-producing actions and query encoding boundaries', async () => {
    const removed = await hostApi.remove(4, 'records_only=true');
    expect(removed).toMatchObject({ path: '/api/nodes/4?records_only=true', options: { method: 'DELETE' } });

    const action = await hostApi.action(4, 'reboot');
    expect(action).toMatchObject({ path: '/api/nodes/4/reboot', options: { method: 'POST' } });

    const maintenance = await hostApi.maintenanceAction('plan-1', 'execute');
    expect(maintenance).toMatchObject({ path: '/api/maintenance/plans/plan-1/execute', options: { method: 'POST' } });
  });

  it('keeps secret-bearing password input inside the JSON request body', async () => {
    const result = await hostApi.testPassword({ address: '192.0.2.10', ssh_user: 'root', ssh_port: 22, password: 'not-recorded' });
    expect(result).toMatchObject({ path: '/api/nodes/test-password', options: { method: 'POST' } });
    expect((result as unknown as { path: string }).path).not.toContain('not-recorded');
  });
});
