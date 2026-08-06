import { describe, expect, it } from 'vitest';
import { activeHostMaintenanceByNode } from './HostsWorkspace';

describe('activeHostMaintenanceByNode', () => {
  it('projects only non-terminal host workflows by their durable target ID', () => {
    const states = activeHostMaintenanceByNode([
      {
        plan_id: 'host-recovery', lifecycle_state: 'recovery_required',
        view: { header: { planId: 'host-recovery', state: 'recovery_required', target: { kind: 'host', id: 4, name: 'node-a' }, operation: 'Host maintenance', reason: '', requester: 'operator', createdAt: '', freshness: { state: 'fresh' }, policy: { name: '', revision: 0, availabilityMode: '' } } },
      },
      {
        plan_id: 'host-complete', lifecycle_state: 'succeeded',
        view: { header: { planId: 'host-complete', state: 'succeeded', target: { kind: 'host', id: 5, name: 'node-b' }, operation: 'Host maintenance', reason: '', requester: 'operator', createdAt: '', freshness: { state: 'fresh' }, policy: { name: '', revision: 0, availabilityMode: '' } } },
      },
      {
        plan_id: 'container-active', lifecycle_state: 'executing',
        view: { header: { planId: 'container-active', state: 'executing', target: { kind: 'container', id: 11, name: 'node-a hot' }, operation: 'Container maintenance', reason: '', requester: 'operator', createdAt: '', freshness: { state: 'fresh' }, policy: { name: '', revision: 0, availabilityMode: '' } } },
      },
    ]);

    expect(states.get(4)?.plan_id).toBe('host-recovery');
    expect(states.has(5)).toBe(false);
    expect(states.has(11)).toBe(false);
  });
});
