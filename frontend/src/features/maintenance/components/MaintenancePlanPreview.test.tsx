import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MaintenancePlanPreview } from './MaintenancePlanPreview';
import type { MaintenancePlanViewModel } from './types';

function plan(overrides: Partial<MaintenancePlanViewModel> = {}): MaintenancePlanViewModel {
  const base: MaintenancePlanViewModel = {
    header: {
      planId: 'plan-host-17',
      state: 'ready',
      target: { kind: 'host', name: 'node-a' },
      operation: 'Reboot host',
      reason: 'Operating-system maintenance',
      requester: 'operator',
      createdAt: '2026-08-03T01:00:00Z',
      freshness: {
        state: 'fresh',
        observedAt: '2026-08-03T01:00:05Z',
        expiresAt: '2026-08-03T01:05:00Z',
      },
      policy: { name: 'Conservative', revision: 4, availabilityMode: 'zero-impact' },
    },
    impact: {
      clusters: [{ id: 1, name: 'search-a' }, { id: 2, name: 'search-b' }],
      workloads: [
        { id: 1, name: 'master-a', role: 'master', host: 'node-a', availability: 'preserved' },
        { id: 2, name: 'hot-a', role: 'hot', host: 'node-a', availability: 'degraded' },
      ],
      endpoints: [
        { id: 1, name: 'Elasticsearch API', availability: 'preserved' },
        { id: 2, name: 'Fleet Server', availability: 'degraded', detail: 'One of two instances remains' },
      ],
      masterQuorum: { availableAfter: 2, total: 3, required: 2, preserved: true },
      dataTiers: [{ tier: 'hot', availableAfter: 2, total: 3, minimumRequired: 1, safe: true }],
      agents: { affected: 1, interruptionExpected: true },
      singletonServices: [{ name: 'Logstash API', estimatedOutage: 'up to 90 seconds' }],
    },
    predicates: [
      { id: 'HostReachable', title: 'Host is reachable', outcome: 'passed', evidence: 'Authenticated SSH probe succeeded.', observedAt: '2026-08-03T01:00:05Z' },
      { id: 'AgentInterruption', title: 'Agent interruption', outcome: 'warning', evidence: 'One agent restarts with its host.', remediation: 'Schedule during a low-volume window.' },
    ],
    steps: [
      { id: 'step-2', sequence: 2, title: 'Restart host', description: 'Invoke the signed one-shot executor.', state: 'pending', checkpoint: { label: 'reboot-invoked' } },
      { id: 'step-1', sequence: 1, title: 'Revalidate safety', description: 'Refresh observations and predicates.', state: 'completed', checkpoint: { label: 'preflight-complete', verifiedAt: '2026-08-03T01:00:10Z' } },
    ],
    lastVerifiedCheckpoint: 'preflight-complete',
  };
  return { ...base, ...overrides };
}

describe('MaintenancePlanPreview', () => {
  afterEach(cleanup);

  it('renders the plan header, aggregate impact, predicate groups, and ordered checkpoints', () => {
    render(<MaintenancePlanPreview plan={plan()} formatTimestamp={(value) => value ? `formatted:${value}` : 'Not available'} />);

    expect(screen.getByRole('heading', { name: 'node-a maintenance plan' })).toBeInTheDocument();
    expect(screen.getByText('2 affected across 2 clusters')).toBeInTheDocument();
    expect(screen.getByText('2 of 3 remain; 2 required')).toBeInTheDocument();
    expect(screen.getByText('up to 90 seconds', { exact: false })).toBeInTheDocument();
    expect(screen.getByText('HostReachable')).toBeInTheDocument();
    expect(screen.getByText('Schedule during a low-volume window.', { exact: false })).toBeInTheDocument();
    expect(screen.getByText('formatted:2026-08-03T01:00:05Z')).toBeInTheDocument();
    expect(screen.getByText('Last verified checkpoint:', { exact: false })).toBeInTheDocument();

    const steps = screen.getByRole('heading', { name: 'Ordered steps and checkpoints' }).parentElement!;
    const titles = within(steps).getAllByRole('heading', { level: 4 }).map((heading) => heading.textContent);
    expect(titles).toEqual(['Revalidate safety', 'Restart host']);
  });

  it('enables execution only for a fresh ready plan without blocking predicates', () => {
    const onAction = vi.fn();
    render(<MaintenancePlanPreview
      plan={plan()}
      actionControls={{ execute: { enabled: true }, cancel: { enabled: true } }}
      onAction={onAction}
    />);

    const execute = screen.getByRole('button', { name: 'Execute plan' });
    expect(execute).not.toBeDisabled();
    fireEvent.click(execute);
    expect(onAction).toHaveBeenCalledWith('execute');
    expect(screen.queryByRole('button', { name: 'Pause' })).not.toBeInTheDocument();
  });

  it('fails closed when a hard predicate blocks execution and shows exact remediation', () => {
    const blocked = plan({
      predicates: [{
        id: 'MasterQuorum',
        title: 'Master quorum is preserved',
        outcome: 'blocking',
        evidence: 'Only one of three master-eligible nodes would remain.',
        remediation: 'Restore a second healthy master-eligible node before retrying.',
        forceable: false,
      }],
    });
    render(<MaintenancePlanPreview
      plan={blocked}
      actionControls={{ execute: { enabled: true }, cancel: { enabled: true } }}
      onAction={() => undefined}
    />);

    expect(screen.getByText('Plan blocked')).toBeInTheDocument();
    expect(screen.getByText('Ready')).toBeInTheDocument();
    expect(screen.getByText('Restore a second healthy master-eligible node before retrying.', { exact: false })).toBeInTheDocument();
    expect(screen.getByText('cannot be overridden', { exact: false })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Execute plan' })).toBeDisabled();
  });

  it('disables execution for stale observations and hides actions explicitly marked invisible', () => {
    const stale = plan({
      header: {
        ...plan().header,
        freshness: { ...plan().header.freshness, state: 'stale', detail: 'Podman stats are older than policy allows.' },
      },
    });
    render(<MaintenancePlanPreview
      plan={stale}
      actionControls={{ execute: { enabled: true }, cancel: { enabled: true, visible: false } }}
      onAction={() => undefined}
    />);

    expect(screen.getByText('Plan requires refresh')).toBeInTheDocument();
    expect(screen.getByText('Podman stats are older than policy allows.', { exact: false })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Execute plan' })).toBeDisabled();
    expect(screen.queryByRole('button', { name: 'Cancel plan' })).not.toBeInTheDocument();
  });

  it('shows only lifecycle-valid recovery actions and never calls browser dialogs', () => {
    const confirm = vi.spyOn(window, 'confirm');
    const onAction = vi.fn();
    const recovery = plan({ header: { ...plan().header, state: 'recovery_required' } });
    render(<MaintenancePlanPreview
      plan={recovery}
      actionControls={{ execute: { enabled: true }, recover: { enabled: true }, cancel: { enabled: false, reason: 'Cleanup must finish first.' } }}
      onAction={onAction}
    />);

    expect(screen.getByRole('button', { name: 'Execute plan' })).toBeDisabled();
    const recover = screen.getByRole('button', { name: 'Review recovery' });
    expect(recover).not.toBeDisabled();
    fireEvent.click(recover);
    expect(onAction).toHaveBeenCalledWith('recover');
    expect(confirm).not.toHaveBeenCalled();
    confirm.mockRestore();
  });
});
