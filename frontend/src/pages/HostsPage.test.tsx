import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ConsoleContext } from '../app-context';
import { HostsPage, maintenancePlanRefetchInterval } from './HostsPage';

const cluster = {
  id: 1, name: 'lab-a', slug: 'lab-a', theme_color: '#0077CC', desired_version: '8.19.0',
  ports: { elasticsearch_http: 9200, elasticsearch_transport: 9300, kibana: 5601, fleet: 8220, logstash_api: 9600 },
  network_defaults: { mode: 'shared' as const },
  zoning: { mode: 'awareness' as const, zones: ['zone-a', 'zone-b'] },
  elasticsearch_settings: { allocation_enable: 'all' as const, rebalance_enable: 'all' as const, disk_watermark_low: '85%', disk_watermark_high: '90%', disk_watermark_flood_stage: '95%', recovery_max_bytes_per_sec: '40mb' },
  members: [], assignments: [],
};

const state = vi.hoisted(() => ({
  nodes: [] as Array<Record<string, unknown>>,
  dashboard: { hosts: [] as Array<Record<string, unknown>> },
  maintenanceCapabilities: {
    planning: false,
    operations: { host_reboot: false, rolling_restart: false, upgrade: false, evacuation: false },
    backends: { documented_rolling: true, node_shutdown: false },
  },
}));
const apiMock = vi.hoisted(() => vi.fn());

type MaintenanceLifecycleState = 'ready' | 'blocked' | 'executing' | 'paused' | 'recovery_required' | 'succeeded' | 'failed' | 'cancelled';

function maintenancePlanResponse(
  lifecycleState: MaintenanceLifecycleState = 'blocked',
  options: {
    safeCheckpoint?: boolean;
    safeCheckpointReason?: string;
    actionControls?: Record<string, { enabled: boolean; visible?: boolean; label?: string; reason?: string; requiresSafeCheckpoint?: boolean }>;
  } = {},
) {
  const predicates = lifecycleState === 'blocked' ? [{
    id: 'MasterQuorum',
    title: 'Master quorum is preserved',
    outcome: 'blocking' as const,
    evidence: 'Only one master-eligible node would remain available.',
    remediation: 'Restore another healthy master-eligible node before retrying.',
    observedAt: '2026-08-03T02:00:00Z',
    forceable: false,
  }] : [{
    id: 'HostReachable',
    title: 'Host is reachable',
    outcome: 'passed' as const,
    evidence: 'Authenticated SSH probe succeeded.',
    observedAt: '2026-08-03T02:00:00Z',
  }];
  return {
    plan_id: 'plan-host-1',
    plan_hash: 'a'.repeat(64),
    lifecycle_state: lifecycleState,
    view: {
      header: {
        planId: 'plan-host-1',
        state: lifecycleState,
        target: { kind: 'host' as const, name: 'node-a' },
        operation: 'Reboot host',
        reason: 'Kernel maintenance',
        requester: 'operator',
        createdAt: '2026-08-03T02:00:00Z',
        freshness: { state: 'fresh' as const, observedAt: '2026-08-03T02:00:00Z', expiresAt: '2026-08-03T02:05:00Z' },
        policy: { name: 'Effective maintenance policy', revision: 0, availabilityMode: 'zero-impact' },
      },
      impact: {
        clusters: [{ id: 1, name: 'lab-a' }],
        workloads: [],
        endpoints: [],
        masterQuorum: { availableAfter: lifecycleState === 'blocked' ? 1 : 2, total: 3, required: 2, preserved: lifecycleState === 'ready' },
        dataTiers: [],
        agents: { affected: 0, interruptionExpected: false },
      },
      predicates,
      steps: [{ id: 'revalidate', sequence: 1, title: 'Revalidate safety', description: 'Refresh observations before any protected side effect.', state: 'pending' as const }],
    },
    operation: {
      progress: {
        lifecycleState,
        progress: { completed: lifecycleState === 'ready' || lifecycleState === 'blocked' ? 0 : 3, total: 7 },
        hostBoot: { state: lifecycleState === 'ready' || lifecycleState === 'blocked' ? 'not_started' : 'waiting_for_return', bootTransitionVerified: false },
        cleanup: [],
        executor: { state: lifecycleState === 'ready' || lifecycleState === 'blocked' ? 'not_staged' : 'running' },
      },
      safe_checkpoint: options.safeCheckpoint ?? true,
      safe_checkpoint_reason: options.safeCheckpointReason || 'The latest persisted checkpoint is an operator action boundary.',
      action_controls: options.actionControls || {},
    },
  };
}

vi.mock('../api', () => ({
  api: apiMock,
  jsonBody: vi.fn((value) => ({ body: JSON.stringify(value), headers: { 'Content-Type': 'application/json' } })),
  queries: {
    nodes: () => Promise.resolve(state.nodes),
    dashboard: () => Promise.resolve(state.dashboard),
    maintenanceCapabilities: () => Promise.resolve(state.maintenanceCapabilities),
  },
}));

describe('HostsPage enrollment', () => {
  afterEach(cleanup);

  beforeEach(() => {
    state.nodes = [];
    state.dashboard = { hosts: [] };
    state.maintenanceCapabilities = {
      planning: false,
      operations: { host_reboot: false, rolling_restart: false, upgrade: false, evacuation: false },
      backends: { documented_rolling: true, node_shutdown: false },
    };
    apiMock.mockReset();
  });

  it('shows host runtime columns and allows IP-only password bootstrap over HTTP', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <ConsoleContext.Provider value={{ clusters: [], selectedClusterId: undefined, setSelectedClusterId: () => undefined, watchRun: () => undefined, refreshAll: async () => undefined }}>
          <HostsPage />
        </ConsoleContext.Provider>
      </QueryClientProvider>,
    );

    expect(screen.getByText('Operating system')).toBeInTheDocument();
    expect(screen.getByText('Podman version')).toBeInTheDocument();
    expect(screen.getByText('Zone')).toBeInTheDocument();
    expect(screen.getByRole('img', { name: 'Podman host server' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Add host' }));
    expect(screen.getByText('SSH host public key (optional)')).toBeInTheDocument();
    expect(screen.getByText(/ELKeeper uses the remote hostname/i)).toBeInTheDocument();
    expect(screen.getByText(/DNS hostnames are rejected/i)).toBeInTheDocument();
    expect(screen.getByRole('radio', { name: 'Bootstrap with a one-time password' })).not.toBeDisabled();
  });

  it('selects a cluster-defined zone while enrolling a host', async () => {
    apiMock.mockResolvedValue({ run_id: 92 });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <ConsoleContext.Provider value={{ clusters: [cluster], selectedCluster: cluster, selectedClusterId: 1, setSelectedClusterId: () => undefined, watchRun: () => undefined, refreshAll: async () => undefined }}>
          <HostsPage />
        </ConsoleContext.Provider>
      </QueryClientProvider>,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Add host' }));
    fireEvent.change(screen.getByLabelText('Inventory name'), { target: { value: 'node-zone-a' } });
    fireEvent.change(screen.getByLabelText('SSH IP address'), { target: { value: '192.0.2.141' } });
    fireEvent.change(screen.getByLabelText('Zone'), { target: { value: 'zone-a' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save host' }));
    await waitFor(() => expect(apiMock).toHaveBeenCalledWith('/api/nodes/enroll', expect.objectContaining({ method: 'POST' })));
    const request = apiMock.mock.calls.find(([path]) => path === '/api/nodes/enroll')?.[1];
    expect(JSON.parse(String(request?.body))).toEqual(expect.objectContaining({ zone_id: 'zone-a', zone_cluster_id: 1 }));
  });

  it('edits a host zone from the inventory action menu', async () => {
    state.nodes = [{ id: 1, name: 'node-a', address: '192.0.2.101', ssh_port: 22, ssh_user: 'root', enabled: true, ssh_auth_state: 'controller_key', zone_id: 'zone-a' }];
    apiMock.mockResolvedValue({ run_id: 93 });
    const watchRun = vi.fn();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <ConsoleContext.Provider value={{ clusters: [cluster], selectedCluster: cluster, selectedClusterId: 1, setSelectedClusterId: () => undefined, watchRun, refreshAll: async () => undefined }}>
          <HostsPage />
        </ConsoleContext.Provider>
      </QueryClientProvider>,
    );
    fireEvent.click(await screen.findByRole('button', { name: 'All actions, row 1' }));
    fireEvent.click(await screen.findByText('Edit zone'));
    fireEvent.change(screen.getByLabelText('Host zone'), { target: { value: 'zone-b' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save zone' }));
    await waitFor(() => expect(apiMock).toHaveBeenCalledWith('/api/nodes/1/zone', expect.objectContaining({ method: 'PUT', body: JSON.stringify({ cluster_id: 1, zone_id: 'zone-b' }) })));
    expect(watchRun).toHaveBeenCalledWith(93);
  });

  it('marks the selected enrollment authentication method as checked', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <ConsoleContext.Provider value={{ clusters: [], selectedClusterId: undefined, setSelectedClusterId: () => undefined, watchRun: () => undefined, refreshAll: async () => undefined }}>
          <HostsPage />
        </ConsoleContext.Provider>
      </QueryClientProvider>,
    );

    fireEvent.click(screen.getByRole('button', { name: 'Add host' }));
    const controllerKey = screen.getByRole('radio', { name: 'Use the configured controller key' });
    const password = screen.getByRole('radio', { name: 'Bootstrap with a one-time password' });

    expect(controllerKey).toBeChecked();
    expect(password).not.toBeChecked();

    fireEvent.click(password);

    expect(password).toBeChecked();
    expect(controllerKey).not.toBeChecked();
  });

  it('tests a password without creating an inventory host', async () => {
    apiMock.mockResolvedValue({ authenticated: true, message: 'Password authentication succeeded.' });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <ConsoleContext.Provider value={{ clusters: [], selectedClusterId: undefined, setSelectedClusterId: () => undefined, watchRun: () => undefined, refreshAll: async () => undefined }}>
          <HostsPage />
        </ConsoleContext.Provider>
      </QueryClientProvider>,
    );

    fireEvent.click(screen.getByRole('button', { name: 'Add host' }));
    fireEvent.change(screen.getByLabelText('SSH IP address'), { target: { value: '192.0.2.101' } });
    fireEvent.click(screen.getByRole('radio', { name: 'Bootstrap with a one-time password' }));
    fireEvent.change(screen.getByLabelText('Host password'), { target: { value: 'one-time-secret' } });
    fireEvent.click(screen.getByRole('button', { name: 'Test password' }));

    await waitFor(() => expect(apiMock).toHaveBeenCalledWith('/api/nodes/test-password', expect.objectContaining({
      method: 'POST', body: JSON.stringify({ address: '192.0.2.101', ssh_user: 'root', ssh_port: 22, ssh_host_key: '', password: 'one-time-secret' }),
    })));
    expect(await screen.findByText('SSH password verified')).toBeInTheDocument();
  });

  it('requires confirmation before starting a host reboot run', async () => {
    state.nodes = [{ id: 1, name: 'node-a', address: '192.0.2.102', ssh_port: 22, ssh_user: 'root', enabled: true, ssh_auth_state: 'controller_key' }];
    state.dashboard = { hosts: [{ id: 1, name: 'node-a', containers: [] }] };
    apiMock.mockResolvedValue({ run_id: 91 });
    const watchRun = vi.fn();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <ConsoleContext.Provider value={{ clusters: [], selectedClusterId: undefined, setSelectedClusterId: () => undefined, watchRun, refreshAll: async () => undefined }}>
          <HostsPage />
        </ConsoleContext.Provider>
      </QueryClientProvider>,
    );

    fireEvent.click(await screen.findByRole('button', { name: 'All actions, row 1' }));
    fireEvent.click(await screen.findByText('Reboot'));
    expect(screen.getByRole('heading', { name: 'Reboot node-a' })).toBeInTheDocument();
    expect(apiMock).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Reboot host' }));
    await waitFor(() => expect(apiMock).toHaveBeenCalledWith('/api/nodes/1/reboot', { method: 'POST' }));
    expect(watchRun).toHaveBeenCalledWith(91);
  });

  it('hides maintenance planning when the backend capability is disabled', async () => {
    state.nodes = [{ id: 1, name: 'node-a', address: '192.0.2.102', ssh_port: 22, ssh_user: 'root', enabled: true, ssh_auth_state: 'controller_key' }];
    state.dashboard = { hosts: [{ id: 1, name: 'node-a', containers: [] }] };
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <ConsoleContext.Provider value={{ clusters: [], selectedClusterId: undefined, setSelectedClusterId: () => undefined, watchRun: () => undefined, refreshAll: async () => undefined }}>
          <HostsPage />
        </ConsoleContext.Provider>
      </QueryClientProvider>,
    );

    fireEvent.click(await screen.findByRole('button', { name: 'All actions, row 1' }));
    expect(screen.queryByText('Plan maintenance')).not.toBeInTheDocument();
    expect(screen.getByText('Reboot')).toBeInTheDocument();
  });

  it('creates and renders a non-mutating blocked maintenance preview', async () => {
    state.nodes = [{ id: 1, name: 'node-a', address: '192.0.2.102', ssh_port: 22, ssh_user: 'root', enabled: true, ssh_auth_state: 'controller_key' }];
    state.dashboard = { hosts: [{ id: 1, name: 'node-a', containers: [] }] };
    state.maintenanceCapabilities.planning = true;
    apiMock.mockResolvedValue(maintenancePlanResponse());
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <ConsoleContext.Provider value={{ clusters: [cluster], selectedCluster: cluster, selectedClusterId: 1, setSelectedClusterId: () => undefined, watchRun: () => undefined, refreshAll: async () => undefined }}>
          <HostsPage />
        </ConsoleContext.Provider>
      </QueryClientProvider>,
    );

    fireEvent.click(await screen.findByRole('button', { name: 'All actions, row 1' }));
    fireEvent.click(await screen.findByText('Plan maintenance'));
    expect(screen.getByRole('heading', { name: 'Plan maintenance for node-a' })).toBeInTheDocument();
    expect(screen.getByText('Planning only')).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Kernel maintenance' } });
    fireEvent.click(screen.getByRole('button', { name: 'Create plan' }));

    await waitFor(() => expect(apiMock).toHaveBeenCalledWith('/api/nodes/1/maintenance/plans', expect.objectContaining({ method: 'POST' })));
    const request = apiMock.mock.calls.find(([path]) => path === '/api/nodes/1/maintenance/plans')?.[1];
    expect(JSON.parse(String(request?.body))).toEqual({
      operation: 'reboot',
      reason: 'Kernel maintenance',
      availability_mode: 'zero-impact',
      idempotency_key: expect.stringMatching(/^host-1-reboot-/),
    });
    expect(await screen.findByText('Plan blocked')).toBeInTheDocument();
    expect(screen.getByText('MasterQuorum')).toBeInTheDocument();
    expect(screen.getByText('Restore another healthy master-eligible node before retrying.', { exact: false })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Execute plan' })).not.toBeInTheDocument();
    expect(apiMock).not.toHaveBeenCalledWith('/api/nodes/1/reboot', expect.anything());
  });

  it('does not synthesize execution controls from the coarse reboot capability flag', async () => {
    state.nodes = [{ id: 1, name: 'node-a', address: '192.0.2.102', ssh_port: 22, ssh_user: 'root', enabled: true, ssh_auth_state: 'controller_key' }];
    state.dashboard = { hosts: [{ id: 1, name: 'node-a', containers: [] }] };
    state.maintenanceCapabilities.planning = true;
    state.maintenanceCapabilities.operations.host_reboot = true;
    apiMock.mockResolvedValue(maintenancePlanResponse('ready'));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <ConsoleContext.Provider value={{ clusters: [cluster], selectedCluster: cluster, selectedClusterId: 1, setSelectedClusterId: () => undefined, watchRun: () => undefined, refreshAll: async () => undefined }}>
          <HostsPage />
        </ConsoleContext.Provider>
      </QueryClientProvider>,
    );

    fireEvent.click(await screen.findByRole('button', { name: 'All actions, row 1' }));
    fireEvent.click(await screen.findByText('Plan maintenance'));
    fireEvent.click(screen.getByRole('button', { name: 'Create plan' }));

    await screen.findByText('Plan ready');
    expect(screen.queryByRole('button', { name: 'Execute plan' })).not.toBeInTheDocument();
    expect(apiMock).toHaveBeenCalledTimes(1);
    expect(apiMock).not.toHaveBeenCalledWith(expect.stringContaining('/execute'), expect.anything());
  });

  it.each([
    ['execute', 'ready', 'Execute plan', true],
    ['pause', 'executing', 'Pause after checkpoint', true],
    ['resume', 'paused', 'Resume maintenance', true],
    ['cancel', 'executing', 'Cancel maintenance', true],
    ['recover', 'recovery_required', 'Recover operation', false],
  ] as const)('runs the server-authorized %s action and hands its run to the action console', async (action, lifecycleState, label, safeCheckpoint) => {
    state.nodes = [{ id: 1, name: 'node-a', address: '192.0.2.102', ssh_port: 22, ssh_user: 'root', enabled: true, ssh_auth_state: 'controller_key' }];
    state.dashboard = { hosts: [{ id: 1, name: 'node-a', containers: [] }] };
    state.maintenanceCapabilities.planning = true;
    state.maintenanceCapabilities.operations.host_reboot = true;
    const plan = maintenancePlanResponse(lifecycleState, {
      safeCheckpoint,
      actionControls: {
        [action]: { enabled: true, ...(action === 'recover' ? { requiresSafeCheckpoint: false } : {}) },
      },
    });
    const runId = 410 + ['execute', 'pause', 'resume', 'cancel', 'recover'].indexOf(action);
    apiMock.mockImplementation((path: string, options?: RequestInit) => {
      if (path === '/api/nodes/1/maintenance/plans' && options?.method === 'POST') return Promise.resolve(plan);
      if (path === `/api/maintenance/plans/plan-host-1/${action}` && options?.method === 'POST') return Promise.resolve({ run_id: runId });
      if (path === '/api/maintenance/plans/plan-host-1') return Promise.resolve(plan);
      return Promise.reject(new Error(`Unexpected API request: ${path}`));
    });
    const watchRun = vi.fn();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <ConsoleContext.Provider value={{ clusters: [cluster], selectedCluster: cluster, selectedClusterId: 1, setSelectedClusterId: () => undefined, watchRun, refreshAll: async () => undefined }}>
          <HostsPage />
        </ConsoleContext.Provider>
      </QueryClientProvider>,
    );

    fireEvent.click(await screen.findByRole('button', { name: 'All actions, row 1' }));
    fireEvent.click(await screen.findByText('Plan maintenance'));
    fireEvent.click(screen.getByRole('button', { name: 'Create plan' }));
    fireEvent.click(await screen.findByRole('button', { name: label }));

    await waitFor(() => expect(apiMock).toHaveBeenCalledWith(`/api/maintenance/plans/plan-host-1/${action}`, { method: 'POST' }));
    expect(watchRun).toHaveBeenCalledWith(runId);
  });

  it('fails closed for server-authorized actions until a safe checkpoint is verified', async () => {
    state.nodes = [{ id: 1, name: 'node-a', address: '192.0.2.102', ssh_port: 22, ssh_user: 'root', enabled: true, ssh_auth_state: 'controller_key' }];
    state.dashboard = { hosts: [{ id: 1, name: 'node-a', containers: [] }] };
    state.maintenanceCapabilities.planning = true;
    state.maintenanceCapabilities.operations.host_reboot = true;
    apiMock.mockResolvedValue(maintenancePlanResponse('executing', {
      safeCheckpoint: false,
      safeCheckpointReason: 'The reboot command was acknowledged and host identity rediscovery is still running.',
      actionControls: { pause: { enabled: true }, cancel: { enabled: true } },
    }));
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <ConsoleContext.Provider value={{ clusters: [cluster], selectedCluster: cluster, selectedClusterId: 1, setSelectedClusterId: () => undefined, watchRun: () => undefined, refreshAll: async () => undefined }}>
          <HostsPage />
        </ConsoleContext.Provider>
      </QueryClientProvider>,
    );

    fireEvent.click(await screen.findByRole('button', { name: 'All actions, row 1' }));
    fireEvent.click(await screen.findByText('Plan maintenance'));
    fireEvent.click(screen.getByRole('button', { name: 'Create plan' }));

    expect(await screen.findByText('Waiting for a safe checkpoint')).toBeInTheDocument();
    expect(screen.getByText('The reboot command was acknowledged and host identity rediscovery is still running.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Pause after checkpoint' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Cancel maintenance' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Cancel maintenance' }));
    expect(apiMock).not.toHaveBeenCalledWith(expect.stringContaining('/cancel'), expect.anything());
  });

  it('polls every nonterminal persisted operation state and stops for terminal states', () => {
    for (const stateName of ['ready', 'blocked', 'executing', 'paused', 'recovery_required'] as const) {
      expect(maintenancePlanRefetchInterval(maintenancePlanResponse(stateName))).toBe(2000);
    }
    for (const stateName of ['succeeded', 'failed', 'cancelled'] as const) {
      expect(maintenancePlanRefetchInterval(maintenancePlanResponse(stateName))).toBe(false);
    }
    const recoveryOverride = maintenancePlanResponse('succeeded');
    recoveryOverride.operation.progress.lifecycleState = 'recovery_required';
    expect(maintenancePlanRefetchInterval(recoveryOverride)).toBe(2000);
    expect(maintenancePlanRefetchInterval(undefined)).toBe(false);
  });

  it('rediscovers persisted recovery state after an ambiguous maintenance action error', async () => {
    state.nodes = [{ id: 1, name: 'node-a', address: '192.0.2.102', ssh_port: 22, ssh_user: 'root', enabled: true, ssh_auth_state: 'controller_key' }];
    state.dashboard = { hosts: [{ id: 1, name: 'node-a', containers: [] }] };
    state.maintenanceCapabilities.planning = true;
    state.maintenanceCapabilities.operations.host_reboot = true;
    const ready = maintenancePlanResponse('ready', { actionControls: { execute: { enabled: true } } });
    const recovery = maintenancePlanResponse('recovery_required', {
      safeCheckpoint: false,
      safeCheckpointReason: 'Remote side effects may have started; inspect persisted evidence before continuing.',
      actionControls: { recover: { enabled: true, requiresSafeCheckpoint: false } },
    });
    apiMock.mockImplementation((path: string, options?: RequestInit) => {
      if (path === '/api/nodes/1/maintenance/plans' && options?.method === 'POST') return Promise.resolve(ready);
      if (path === '/api/maintenance/plans/plan-host-1/execute') return Promise.reject(new Error('The controller lost the action response.'));
      if (path === '/api/maintenance/plans/plan-host-1') return Promise.resolve(recovery);
      return Promise.reject(new Error(`Unexpected API request: ${path}`));
    });
    const watchRun = vi.fn();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <ConsoleContext.Provider value={{ clusters: [cluster], selectedCluster: cluster, selectedClusterId: 1, setSelectedClusterId: () => undefined, watchRun, refreshAll: async () => undefined }}>
          <HostsPage />
        </ConsoleContext.Provider>
      </QueryClientProvider>,
    );

    fireEvent.click(await screen.findByRole('button', { name: 'All actions, row 1' }));
    fireEvent.click(await screen.findByText('Plan maintenance'));
    fireEvent.click(screen.getByRole('button', { name: 'Create plan' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Execute plan' }));

    expect(await screen.findByText('Maintenance request failed')).toBeInTheDocument();
    expect(screen.getByText('The controller lost the action response.')).toBeInTheDocument();
    expect(await screen.findByText('Recovery decision required')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Recover operation' })).toBeEnabled();
    expect(watchRun).not.toHaveBeenCalled();
  });

  it('keeps the last persisted evidence visible and offers retry when status refresh fails', async () => {
    state.nodes = [{ id: 1, name: 'node-a', address: '192.0.2.102', ssh_port: 22, ssh_user: 'root', enabled: true, ssh_auth_state: 'controller_key' }];
    state.dashboard = { hosts: [{ id: 1, name: 'node-a', containers: [] }] };
    state.maintenanceCapabilities.planning = true;
    state.maintenanceCapabilities.operations.host_reboot = true;
    const ready = maintenancePlanResponse('ready', { actionControls: { execute: { enabled: true } } });
    let statusRequests = 0;
    apiMock.mockImplementation((path: string, options?: RequestInit) => {
      if (path === '/api/nodes/1/maintenance/plans' && options?.method === 'POST') return Promise.resolve(ready);
      if (path === '/api/maintenance/plans/plan-host-1/execute') return Promise.resolve({ run_id: 419 });
      if (path === '/api/maintenance/plans/plan-host-1') {
        statusRequests += 1;
        return statusRequests === 1
          ? Promise.reject(new Error('Persisted status is temporarily unavailable.'))
          : Promise.resolve(ready);
      }
      return Promise.reject(new Error(`Unexpected API request: ${path}`));
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <ConsoleContext.Provider value={{ clusters: [cluster], selectedCluster: cluster, selectedClusterId: 1, setSelectedClusterId: () => undefined, watchRun: () => undefined, refreshAll: async () => undefined }}>
          <HostsPage />
        </ConsoleContext.Provider>
      </QueryClientProvider>,
    );

    fireEvent.click(await screen.findByRole('button', { name: 'All actions, row 1' }));
    fireEvent.click(await screen.findByText('Plan maintenance'));
    fireEvent.click(screen.getByRole('button', { name: 'Create plan' }));
    fireEvent.click(await screen.findByRole('button', { name: 'Execute plan' }));

    expect(await screen.findByText('Maintenance status refresh failed')).toBeInTheDocument();
    expect(screen.getByText('Persisted status is temporarily unavailable.')).toBeInTheDocument();
    expect(screen.getByText('Plan ready')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Retry status refresh' }));
    await waitFor(() => expect(screen.queryByText('Maintenance status refresh failed')).not.toBeInTheDocument());
    expect(statusRequests).toBe(2);
  });

  it('removes inherited legacy known_hosts trust after confirmation', async () => {
    state.nodes = [{ id: 1, name: 'node-a', address: '192.0.2.101', ssh_port: 22, ssh_user: 'root', enabled: true, ssh_auth_state: 'legacy' }];
    apiMock.mockResolvedValue({ updated: true, legacy_known_hosts_disabled: true });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <ConsoleContext.Provider value={{ clusters: [], selectedClusterId: undefined, setSelectedClusterId: () => undefined, watchRun: () => undefined, refreshAll: async () => undefined }}>
          <HostsPage />
        </ConsoleContext.Provider>
      </QueryClientProvider>,
    );

    fireEvent.click(await screen.findByRole('button', { name: 'All actions, row 1' }));
    fireEvent.click(await screen.findByText('Remove legacy known_hosts record'));
    expect(screen.getByRole('heading', { name: 'Remove legacy known_hosts record node-a' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Remove legacy record' }));
    await waitFor(() => expect(apiMock).toHaveBeenCalledWith('/api/nodes/1/legacy-known-hosts/remove', { method: 'POST' }));
  });

  it('defaults an unreachable controller-key host to records-only deletion', async () => {
    state.nodes = [{ id: 1, name: 'node-a', address: '192.0.2.101', ssh_port: 22, ssh_user: 'root', enabled: true, ssh_auth_state: 'controller_key' }];
    state.dashboard = { hosts: [{ id: 1, name: 'node-a', reachable: false, last_error: 'SSH: Controller SSH key authentication failed', containers: [] }] };
    apiMock.mockResolvedValue(undefined);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <ConsoleContext.Provider value={{ clusters: [], selectedClusterId: undefined, setSelectedClusterId: () => undefined, watchRun: () => undefined, refreshAll: async () => undefined }}>
          <HostsPage />
        </ConsoleContext.Provider>
      </QueryClientProvider>,
    );

    fireEvent.click(await screen.findByRole('button', { name: 'All actions, row 1' }));
    fireEvent.click(await screen.findByText('Delete'));
    expect(screen.getByRole('switch', { name: 'Remove the installed controller key before deleting this host' })).not.toBeChecked();
    expect(screen.getByText(/Controller key will remain on the host/i)).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Delete record only' }));
    await waitFor(() => expect(apiMock).toHaveBeenCalledWith('/api/nodes/1?records_only=true', { method: 'DELETE' }));
  });

  it('defaults a host with no runtime observation to records-only deletion', async () => {
    state.nodes = [{ id: 1, name: 'node-a', address: '192.0.2.101', ssh_port: 22, ssh_user: 'root', enabled: true, ssh_auth_state: 'controller_key' }];
    apiMock.mockResolvedValue(undefined);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <ConsoleContext.Provider value={{ clusters: [], selectedClusterId: undefined, setSelectedClusterId: () => undefined, watchRun: () => undefined, refreshAll: async () => undefined }}>
          <HostsPage />
        </ConsoleContext.Provider>
      </QueryClientProvider>,
    );

    fireEvent.click(await screen.findByRole('button', { name: 'All actions, row 1' }));
    fireEvent.click(await screen.findByText('Delete'));
    expect(screen.getByRole('switch', { name: 'Remove the installed controller key before deleting this host' })).not.toBeChecked();
    fireEvent.click(screen.getByRole('button', { name: 'Delete record only' }));
    await waitFor(() => expect(apiMock).toHaveBeenCalledWith('/api/nodes/1?records_only=true', { method: 'DELETE' }));
  });

  it('uses records-only deletion even when host key state is unknown', async () => {
    state.nodes = [{ id: 1, name: 'node-a', address: '192.0.2.101', ssh_port: 22, ssh_user: 'root', enabled: false, ssh_auth_state: 'pending' }];
    apiMock.mockResolvedValue(undefined);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <ConsoleContext.Provider value={{ clusters: [], selectedClusterId: undefined, setSelectedClusterId: () => undefined, watchRun: () => undefined, refreshAll: async () => undefined }}>
          <HostsPage />
        </ConsoleContext.Provider>
      </QueryClientProvider>,
    );

    fireEvent.click(await screen.findByRole('button', { name: 'All actions, row 1' }));
    fireEvent.click(await screen.findByText('Delete'));
    fireEvent.click(screen.getByRole('button', { name: 'Delete record only' }));
    await waitFor(() => expect(apiMock).toHaveBeenCalledWith('/api/nodes/1?records_only=true', { method: 'DELETE' }));
  });

  it('requires explicit key-revocation selection even when a host is reachable', async () => {
    state.nodes = [{ id: 1, name: 'node-a', address: '192.0.2.101', ssh_port: 22, ssh_user: 'root', enabled: true, ssh_auth_state: 'controller_key' }];
    state.dashboard = { hosts: [{ id: 1, name: 'node-a', reachable: true, containers: [] }] };
    apiMock.mockResolvedValue(undefined);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <ConsoleContext.Provider value={{ clusters: [], selectedClusterId: undefined, setSelectedClusterId: () => undefined, watchRun: () => undefined, refreshAll: async () => undefined }}>
          <HostsPage />
        </ConsoleContext.Provider>
      </QueryClientProvider>,
    );

    fireEvent.click(await screen.findByRole('button', { name: 'All actions, row 1' }));
    fireEvent.click(await screen.findByText('Delete'));
    expect(screen.getByRole('switch', { name: 'Remove the installed controller key before deleting this host' })).not.toBeChecked();
    fireEvent.click(screen.getByRole('button', { name: 'Delete record only' }));
    await waitFor(() => expect(apiMock).toHaveBeenCalledWith('/api/nodes/1?records_only=true', { method: 'DELETE' }));
  });

  it('hides De-initialize for an uninitialized host', async () => {
    state.nodes = [{ id: 1, name: 'node-a', address: '192.0.2.101', ssh_port: 22, ssh_user: 'root', enabled: true, ssh_auth_state: 'controller_key' }];
    state.dashboard = { hosts: [{ id: 1, name: 'node-a', initialized: false, containers: [] }] };
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <ConsoleContext.Provider value={{ clusters: [], selectedClusterId: undefined, setSelectedClusterId: () => undefined, watchRun: () => undefined, refreshAll: async () => undefined }}>
          <HostsPage />
        </ConsoleContext.Provider>
      </QueryClientProvider>,
    );

    fireEvent.click(await screen.findByRole('button', { name: 'All actions, row 1' }));
    expect(screen.queryByRole('menuitem', { name: 'De-initialize' })).not.toBeInTheDocument();
  });
});
