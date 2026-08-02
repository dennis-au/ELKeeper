import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ConsoleContext } from '../app-context';
import { HostsPage } from './HostsPage';

const cluster = {
  id: 1, name: 'lab-a', slug: 'lab-a', theme_color: '#0077CC', desired_version: '8.19.0',
  ports: { elasticsearch_http: 9200, elasticsearch_transport: 9300, kibana: 5601, fleet: 8220, logstash_api: 9600 },
  network_defaults: { mode: 'shared' as const },
  zoning: { mode: 'awareness' as const, zones: ['zone-a', 'zone-b'] },
  elasticsearch_settings: { allocation_enable: 'all' as const, rebalance_enable: 'all' as const, disk_watermark_low: '85%', disk_watermark_high: '90%', disk_watermark_flood_stage: '95%', recovery_max_bytes_per_sec: '40mb' },
  members: [], assignments: [],
};

const state = vi.hoisted(() => ({ nodes: [] as Array<Record<string, unknown>>, dashboard: { hosts: [] as Array<Record<string, unknown>> } }));
const apiMock = vi.hoisted(() => vi.fn());

vi.mock('../api', () => ({
  api: apiMock,
  jsonBody: vi.fn((value) => ({ body: JSON.stringify(value), headers: { 'Content-Type': 'application/json' } })),
  queries: {
    nodes: () => Promise.resolve(state.nodes),
    dashboard: () => Promise.resolve(state.dashboard),
  },
}));

describe('HostsPage enrollment', () => {
  afterEach(cleanup);

  beforeEach(() => {
    state.nodes = [];
    state.dashboard = { hosts: [] };
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
