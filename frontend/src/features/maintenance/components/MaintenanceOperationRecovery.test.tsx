import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MaintenanceOperationActions } from './MaintenanceOperationActions';
import { MaintenanceOperationProgress } from './MaintenanceOperationProgress';
import type { MaintenanceOperationProgressModel } from './MaintenanceOperationProgress';

const progress: MaintenanceOperationProgressModel = {
  lifecycleState: 'recovery_required',
  progress: { completed: 5, total: 8 },
  activeCheckpoint: {
    id: 'restore-allocation',
    label: 'Restore Elasticsearch allocation settings',
    state: 'recovery_required',
    safeForOperatorAction: false,
    detail: 'Persistent allocation was restored, but the transient layer has not been verified.',
    updatedAt: '2026-08-03T04:12:00Z',
  },
  lastVerifiedCheckpoint: {
    label: 'host-returned',
    verifiedAt: '2026-08-03T04:10:00Z',
  },
  hostBoot: {
    state: 'returned',
    bootTransitionVerified: true,
    observedAt: '2026-08-03T04:10:00Z',
    detail: 'The host returned with a different boot identity and systemd is available.',
  },
  cleanup: [
    {
      id: 'allocation-search-a',
      kind: 'allocation',
      clusterName: 'search-a',
      state: 'unresolved',
      detail: 'Transient cluster.routing.allocation.enable still differs from the captured value.',
      updatedAt: '2026-08-03T04:12:00Z',
    },
    {
      id: 'shutdown-search-b',
      kind: 'shutdown',
      clusterName: 'search-b',
      state: 'restored',
      detail: 'The temporary restart shutdown record is absent.',
      updatedAt: '2026-08-03T04:11:00Z',
    },
  ],
  executor: {
    state: 'recovery_required',
    signatureVerified: true,
    resultIdentityVerified: true,
    resultImported: true,
    reason: 'One bounded cleanup check could not prove restoration.',
    observedAt: '2026-08-03T04:11:30Z',
    checks: [
      { id: 'quadlets', label: 'Previously running Quadlets returned', state: 'passed' },
      { id: 'allocation', label: 'Allocation settings restored', state: 'failed', detail: 'Transient layer mismatch' },
    ],
  },
};

describe('MaintenanceOperationProgress', () => {
  afterEach(cleanup);

  it('presents checkpoint, boot, cleanup, and executor identity evidence as one accessible status region', () => {
    render(<MaintenanceOperationProgress progress={progress} formatTimestamp={(value) => value ? `time:${value}` : 'Not available'} />);

    const region = screen.getByRole('region', { name: 'Maintenance operation progress' });
    expect(within(region).getByRole('heading', { name: 'Operation progress and recovery evidence' })).toBeInTheDocument();
    expect(within(region).getByText('5 of 8 steps complete')).toBeInTheDocument();
    expect(within(region).getByText('Restore Elasticsearch allocation settings')).toBeInTheDocument();
    expect(within(region).getByText('host-returned', { exact: false })).toBeInTheDocument();
    expect(within(region).getByText('Boot transition verified')).toBeInTheDocument();
    expect(within(region).getByText('search-a')).toBeInTheDocument();
    expect(within(region).getByText('Transient cluster.routing.allocation.enable still differs from the captured value.', { exact: false })).toBeInTheDocument();
    expect(within(region).getByText('Manifest signature verified')).toBeInTheDocument();
    expect(within(region).getByText('Executor result identity verified')).toBeInTheDocument();
    expect(within(region).getByText('Allocation settings restored')).toBeInTheDocument();
    expect(within(region).getByText('time:2026-08-03T04:12:00Z')).toBeInTheDocument();
    expect(within(within(region).getByRole('list', { name: 'Temporary cleanup evidence' })).getByText('time:2026-08-03T04:12:00Z', { exact: false })).toBeInTheDocument();
  });

  it('shows explicit unknown and empty evidence states instead of a blank panel', () => {
    render(<MaintenanceOperationProgress progress={{
      lifecycleState: 'executing',
      hostBoot: { state: 'unknown', bootTransitionVerified: false },
      cleanup: [],
      executor: { state: 'unavailable' },
    }} />);

    expect(screen.getByText('Checkpoint evidence has not been recorded yet.')).toBeInTheDocument();
    expect(screen.getByText('Host boot state is unknown')).toBeInTheDocument();
    expect(screen.getByText('No temporary allocation or shutdown cleanup is recorded.')).toBeInTheDocument();
    expect(screen.getByText('Executor evidence is unavailable')).toBeInTheDocument();
  });

  it('uses responsive description layouts for narrow viewports', () => {
    const { container } = render(<MaintenanceOperationProgress progress={progress} />);

    const responsiveLists = container.querySelectorAll('dl[data-type="responsiveColumn"]');
    expect(responsiveLists.length).toBeGreaterThanOrEqual(3);
    expect(screen.getByRole('region', { name: 'Maintenance operation progress' })).toBeInTheDocument();
  });
});

describe('MaintenanceOperationActions', () => {
  afterEach(cleanup);

  it('shows only lifecycle-valid actions and invokes them without browser-native dialogs', () => {
    const onAction = vi.fn();
    const confirm = vi.spyOn(window, 'confirm');
    const alert = vi.spyOn(window, 'alert');
    const prompt = vi.spyOn(window, 'prompt');
    render(<MaintenanceOperationActions
      lifecycleState="executing"
      safeCheckpoint
      controls={{
        pause: { enabled: true },
        resume: { enabled: true },
        cancel: { enabled: true },
        recover: { enabled: true },
      }}
      onAction={onAction}
    />);

    const actions = screen.getByRole('group', { name: 'Maintenance operation actions' });
    expect(within(actions).getByRole('button', { name: 'Pause after checkpoint' })).toBeEnabled();
    expect(within(actions).getByRole('button', { name: 'Cancel maintenance' })).toBeEnabled();
    expect(within(actions).queryByRole('button', { name: 'Resume maintenance' })).not.toBeInTheDocument();
    expect(within(actions).queryByRole('button', { name: 'Recover operation' })).not.toBeInTheDocument();

    const pause = within(actions).getByRole('button', { name: 'Pause after checkpoint' });
    pause.focus();
    expect(pause).toHaveFocus();
    fireEvent.click(pause);
    expect(onAction).toHaveBeenCalledWith('pause');
    expect(confirm).not.toHaveBeenCalled();
    expect(alert).not.toHaveBeenCalled();
    expect(prompt).not.toHaveBeenCalled();
    confirm.mockRestore();
    alert.mockRestore();
    prompt.mockRestore();
  });

  it('fails closed at an unsafe checkpoint and explains why actions are disabled', () => {
    const onAction = vi.fn();
    render(<MaintenanceOperationActions
      lifecycleState="executing"
      safeCheckpoint={false}
      safeCheckpointReason="The host reboot is active and cannot be interrupted."
      controls={{ pause: { enabled: true }, cancel: { enabled: true } }}
      onAction={onAction}
    />);

    expect(screen.getByText('Waiting for a safe checkpoint')).toBeInTheDocument();
    expect(screen.getByText('The host reboot is active and cannot be interrupted.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Pause after checkpoint' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Cancel maintenance' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Cancel maintenance' }));
    expect(onAction).not.toHaveBeenCalled();
  });

  it('shows recovery controls only in recovery-required state and respects server authorization', () => {
    render(<MaintenanceOperationActions
      lifecycleState="recovery_required"
      safeCheckpoint
      controls={{
        recover: { enabled: true, label: 'Review and recover' },
        cancel: { enabled: false, reason: 'Restore allocation settings before cancellation.' },
        pause: { enabled: true },
      }}
      onAction={() => undefined}
    />);

    expect(screen.getByRole('button', { name: 'Review and recover' })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Cancel maintenance' })).toBeDisabled();
    expect(screen.queryByRole('button', { name: 'Pause after checkpoint' })).not.toBeInTheDocument();
    expect(screen.getByText('Restore allocation settings before cancellation.')).toBeInTheDocument();
  });

  it('renders no action surface for terminal lifecycle states or absent controls', () => {
    const { rerender } = render(<MaintenanceOperationActions lifecycleState="succeeded" safeCheckpoint controls={{ recover: { enabled: true } }} />);
    expect(screen.queryByRole('group', { name: 'Maintenance operation actions' })).not.toBeInTheDocument();

    rerender(<MaintenanceOperationActions lifecycleState="paused" safeCheckpoint />);
    expect(screen.queryByRole('group', { name: 'Maintenance operation actions' })).not.toBeInTheDocument();
  });

  it('disables every visible action while another request is pending', () => {
    render(<MaintenanceOperationActions
      lifecycleState="paused"
      safeCheckpoint
      busyAction="resume"
      controls={{ resume: { enabled: true }, cancel: { enabled: true } }}
      onAction={() => undefined}
    />);

    expect(screen.getByRole('button', { name: 'Resume maintenance' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Cancel maintenance' })).toBeDisabled();
  });
});
