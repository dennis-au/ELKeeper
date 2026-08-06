import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ConsoleContext } from '../../app-context';
import { MaintenanceWorkspace } from './MaintenanceWorkspace';

const state = vi.hoisted(() => ({
  enter: vi.fn(),
  exit: vi.fn(),
  preview: vi.fn(),
  getPlan: vi.fn(),
  action: vi.fn(),
  containerWorkflowAction: vi.fn(),
  hostWorkflowAction: vi.fn(),
  capabilities: { planning: true, operations: { manual_maintenance_entry: true, container_stop: false, host_shutdown: false, host_reboot: false, rolling_restart: false, upgrade: false, evacuation: false }, lifecycle: { manual_maintenance_exit: true, recovery: true }, backends: { documented_rolling: true, node_shutdown: false } },
}));

vi.mock('../hosts', () => ({
  hostApi: { list: () => Promise.resolve([{ id: 1, name: 'node-a', address: '192.0.2.10', ssh_port: 22, ssh_user: 'root', enabled: true }]) },
}));

vi.mock('./api', () => ({
  maintenanceApi: {
    capabilities: () => Promise.resolve(state.capabilities),
    manualMode: () => Promise.resolve({ node_id: 1, state: 'available', state_revision: 1, plan_id: null, run_id: null, expires_at: null, lifecycle_state: null }),
    listPlans: () => Promise.resolve({ count: 1, items: [{ plan_id: 'plan-1', lifecycle_state: 'ready', view: { header: { planId: 'plan-1', state: 'ready', target: { kind: 'host', name: 'node-a' }, operation: 'Manual maintenance', reason: 'Inspection', requester: 'operator', createdAt: '2026-08-04T00:00:00Z', freshness: { state: 'fresh' }, policy: { name: 'Effective maintenance policy', revision: 1, availabilityMode: 'zero-impact' } } } }] }),
    enterManualMode: state.enter,
    exitManualMode: state.exit,
    preview: state.preview,
    getPlan: state.getPlan,
    action: state.action,
    containerWorkflowAction: state.containerWorkflowAction,
    hostWorkflowAction: state.hostWorkflowAction,
  },
}));

const cluster = {
  id: 1, name: 'lab-a', slug: 'lab-a', theme_color: '#0077CC', desired_version: '8.20.0',
  ports: { elasticsearch_http: 9200, elasticsearch_transport: 9300, kibana: 5601, fleet: 8220, logstash_api: 9600 },
  network_defaults: { mode: 'shared' as const }, elasticsearch_settings: { allocation_enable: 'all' as const, rebalance_enable: 'all' as const, disk_watermark_low: '85%', disk_watermark_high: '90%', disk_watermark_flood_stage: '95%', recovery_max_bytes_per_sec: '40mb' },
  members: [{ cluster_id: 1, node_id: 1, name: 'node-a', address: '192.0.2.10', enabled: true, network_mode: 'shared' as const, data_interface: 'ens18', data_address: '192.0.2.10', user_interface: 'ens18', user_address: '192.0.2.10', network_ready: true }],
  assignments: [{ id: 7, cluster_id: 1, node_id: 1, node_name: 'node-a', role: 'hot', state: 'active', revision: 1, config: {} }],
};

function renderWorkspace() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}><ConsoleContext.Provider value={{ clusters: [cluster], selectedCluster: cluster, selectedClusterId: 1, setSelectedClusterId: () => undefined, watchRun: () => undefined, refreshAll: async () => undefined }}><MaintenanceWorkspace /></ConsoleContext.Provider></QueryClientProvider>);
}

describe('MaintenanceWorkspace', () => {
  afterEach(() => {
    cleanup();
  });

  beforeEach(() => {
    state.capabilities.operations.manual_maintenance_entry = true;
    state.capabilities.operations.container_stop = false;
    state.capabilities.operations.host_reboot = false;
    state.preview.mockReset();
    state.getPlan.mockReset();
    state.action.mockReset();
    state.containerWorkflowAction.mockReset();
    state.hostWorkflowAction.mockReset();
  });

  it('shows selected-cluster plan history and opens an in-page manual-maintenance form', async () => {
    state.enter.mockResolvedValue({ node_id: 1, state: 'maintenance', state_revision: 2, plan_id: 'plan-2', run_id: null, expires_at: null, lifecycle_state: 'ready' });
    renderWorkspace();
    expect(await screen.findByRole('heading', { name: 'Plan history' })).toBeInTheDocument();
    expect(screen.getByText('plan-1')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Enter maintenance mode' }));
    expect(screen.getByRole('heading', { name: 'Enter maintenance mode' })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Manual maintenance reason'), { target: { value: 'Kernel patching' } });
    fireEvent.click(screen.getAllByRole('button', { name: 'Enter maintenance mode' })[1]);
    await waitFor(() => expect(state.enter).toHaveBeenCalledWith(1, expect.objectContaining({ reason: 'Kernel patching', duration_seconds: 3600 })));
  });

  it('does not allow manual entry merely because planning is enabled', async () => {
    state.capabilities.operations.manual_maintenance_entry = false;
    renderWorkspace();

    const enter = await screen.findByRole('button', { name: 'Enter maintenance mode' });
    expect(enter).toBeDisabled();
  });

  it('creates an exact read-only container maintenance preview', async () => {
    const plan = {
      plan_id: 'container-plan', lifecycle_state: 'blocked',
      view: {
        header: { planId: 'container-plan', state: 'blocked', target: { kind: 'container', name: 'node-a hot' }, operation: 'Container maintenance', reason: 'Inspect one workload', requester: 'operator', createdAt: '2026-08-05T00:00:00Z', freshness: { state: 'fresh' }, policy: { name: 'Effective maintenance policy', revision: 1, availabilityMode: 'zero-impact' } },
        impact: { clusters: [{ id: 1, name: 'lab-a' }], workloads: [{ id: 7, name: 'node-a hot', role: 'hot', host: 'node-a', availability: 'degraded' }], endpoints: [], dataTiers: [], agents: { affected: 0, interruptionExpected: false } },
        predicates: [{ id: 'HostReachable', title: 'Host reachable', outcome: 'passed', evidence: 'Host observation is fresh.' }],
        steps: [{ id: 'preview:1', sequence: 1, title: 'Refresh observations', description: 'Refresh observations.', state: 'pending' }],
      },
    };
    state.preview.mockResolvedValue(plan);
    state.getPlan.mockResolvedValue(plan);
    renderWorkspace();

    fireEvent.click(await screen.findByRole('button', { name: 'Container' }));
    fireEvent.change(screen.getByLabelText('Maintenance workload'), { target: { value: '7' } });
    fireEvent.change(screen.getByLabelText('Preview reason'), { target: { value: 'Inspect one workload' } });
    fireEvent.click(screen.getByRole('button', { name: 'Create preview' }));

    await waitFor(() => expect(state.preview).toHaveBeenCalledWith(expect.objectContaining({
      operation: 'container_maintenance', assignment_id: 7, reason: 'Inspect one workload',
    })));
    expect(await screen.findByRole('heading', { name: 'Maintenance plan container-plan' })).toBeInTheDocument();
  });

  it('renders persisted recovery evidence and uses the recovery action without host execution capability', async () => {
    state.getPlan.mockResolvedValue({
      plan_id: 'plan-1', lifecycle_state: 'recovery_required', run_id: 17,
      view: {
        header: { planId: 'plan-1', state: 'recovery_required', target: { kind: 'host', id: 1, name: 'node-a' }, operation: 'Host maintenance', reason: 'Kernel patching', requester: 'operator', createdAt: '2026-08-05T00:00:00Z', freshness: { state: 'expired' }, policy: { name: 'Effective maintenance policy', revision: 1, availabilityMode: 'zero-impact' } },
        impact: { clusters: [{ id: 1, name: 'lab-a' }], workloads: [{ id: 7, name: 'node-a hot', role: 'hot', host: 'node-a', availability: 'degraded' }], endpoints: [], dataTiers: [], agents: { affected: 0, interruptionExpected: false } },
        predicates: [], steps: [],
      },
      operation: {
        progress: {
          lifecycleState: 'recovery_required', progress: { completed: 2, total: 4 },
          activeCheckpoint: { id: 'host:stop:11', label: 'Stop hot workload', state: 'recovery_required', safeForOperatorAction: false, detail: 'Rediscovery is required.', updatedAt: '2026-08-05T00:01:00Z' },
          hostBoot: { state: 'unknown', bootTransitionVerified: false }, cleanup: [], executor: { state: 'unavailable' },
        },
        safe_checkpoint: false,
        safe_checkpoint_reason: 'Rediscovery is required.',
        action_controls: { recover: { enabled: true, requiresSafeCheckpoint: false, label: 'Recover operation' } },
      },
    });
    state.action.mockResolvedValue({ run_id: 17 });
    renderWorkspace();

    fireEvent.click(await screen.findByRole('button', { name: 'plan-1' }));
    expect(await screen.findByRole('heading', { name: 'Operation progress and recovery evidence' })).toBeInTheDocument();
    const recover = screen.getByRole('button', { name: 'Recover operation' });
    expect(recover).toBeEnabled();
    fireEvent.click(recover);

    await waitFor(() => expect(state.action).toHaveBeenCalledWith('plan-1', 'recover'));
  });

  it('dispatches the next host workflow action only after in-page confirmation', async () => {
    state.capabilities.operations.host_reboot = true;
    state.getPlan.mockResolvedValue({
      plan_id: 'plan-1', lifecycle_state: 'ready', run_id: null,
      view: {
        header: { planId: 'plan-1', state: 'ready', target: { kind: 'host', id: 1, name: 'node-a' }, operation: 'Host maintenance', reason: 'Kernel patching', requester: 'operator', createdAt: '2026-08-05T00:00:00Z', freshness: { state: 'fresh' }, policy: { name: 'Effective maintenance policy', revision: 1, availabilityMode: 'zero-impact' } },
        impact: { clusters: [{ id: 1, name: 'lab-a' }], workloads: [], endpoints: [], dataTiers: [], agents: { affected: 0, interruptionExpected: false } }, predicates: [], steps: [],
      },
      operation: {
        progress: { lifecycleState: 'ready', workflowState: 'available', workflowScope: 'host_maintenance', hostBoot: { state: 'not_started', bootTransitionVerified: false }, cleanup: [], executor: { state: 'not_staged' } },
        safe_checkpoint: true, safe_checkpoint_reason: 'No protected side effect has started.', action_controls: {},
      },
    });
    state.hostWorkflowAction.mockResolvedValue({ plan_id: 'plan-1', run_id: 23, action: 'prepare', workflow_state: 'ready_to_stop', lifecycle_state: 'executing' });
    renderWorkspace();

    fireEvent.click(await screen.findByRole('button', { name: 'plan-1' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Prepare host' }));
    expect(state.hostWorkflowAction).not.toHaveBeenCalled();
    fireEvent.click(screen.getAllByRole('button', { name: 'Prepare host' })[1]);

    await waitFor(() => expect(state.hostWorkflowAction).toHaveBeenCalledWith('plan-1', 'prepare'));
  });

  it('dispatches the next container workflow action to the isolated container endpoint', async () => {
    state.capabilities.operations.container_stop = true;
    state.getPlan.mockResolvedValue({
      plan_id: 'plan-1', lifecycle_state: 'executing', run_id: 23,
      view: {
        header: { planId: 'plan-1', state: 'executing', target: { kind: 'container', id: 7, name: 'node-a hot' }, operation: 'Container maintenance', reason: 'Restart hot workload', requester: 'operator', createdAt: '2026-08-05T00:00:00Z', freshness: { state: 'fresh' }, policy: { name: 'Effective maintenance policy', revision: 1, availabilityMode: 'zero-impact' } },
        impact: { clusters: [{ id: 1, name: 'lab-a' }], workloads: [], endpoints: [], dataTiers: [], agents: { affected: 0, interruptionExpected: false } }, predicates: [], steps: [],
      },
      operation: {
        progress: { lifecycleState: 'executing', workflowState: 'ready_to_stop', workflowScope: 'container_maintenance', hostBoot: { state: 'not_started', bootTransitionVerified: false }, cleanup: [], executor: { state: 'unavailable' } },
        safe_checkpoint: true, safe_checkpoint_reason: 'No protected side effect has started.', action_controls: {},
      },
    });
    state.containerWorkflowAction.mockResolvedValue({ plan_id: 'plan-1', run_id: 23, action: 'stop', workflow_state: 'maintenance', lifecycle_state: 'executing' });
    renderWorkspace();

    fireEvent.click(await screen.findByRole('button', { name: 'plan-1' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Stop workload' }));
    fireEvent.click(screen.getAllByRole('button', { name: 'Stop workload' })[1]);

    await waitFor(() => expect(state.containerWorkflowAction).toHaveBeenCalledWith('plan-1', 'stop'));
  });
});
