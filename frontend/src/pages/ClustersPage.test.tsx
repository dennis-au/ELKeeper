import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ConsoleContext } from '../app-context';
import type { Cluster } from '../types';
import { ClustersPage } from './ClustersPage';

const state = vi.hoisted(() => ({
  maintenanceCapabilities: {
    planning: false,
    operations: { host_reboot: false, rolling_restart: false, upgrade: false, evacuation: false },
    backends: { documented_rolling: true, node_shutdown: false },
  },
  api: vi.fn<(path: string, options?: RequestInit) => Promise<unknown>>((path) => {
    if (path === '/api/clusters/1/versions') return Promise.resolve({ available_versions: [], assignments: [] });
    if (path === '/api/clusters/1/log-monitoring') return Promise.resolve({ run_id: 55 });
    return Promise.resolve({ id: 2 });
  }),
}));

vi.mock('../api', () => ({
  api: state.api,
  jsonBody: vi.fn((value) => ({ body: JSON.stringify(value), headers: { 'Content-Type': 'application/json' } })),
  queries: { maintenanceCapabilities: () => Promise.resolve(state.maintenanceCapabilities) },
}));

const cluster: Cluster = {
  id: 1, name: 'lab-a', slug: 'lab-a', theme_color: '#0077CC', desired_version: '8.19.0',
  ports: { elasticsearch_http: 9200, elasticsearch_transport: 9300, kibana: 5601, fleet: 8220, logstash_api: 9600 },
  network_defaults: { mode: 'shared' },
  zoning: { mode: 'disabled', zones: [] },
  zoning_status: { applied_mode: 'disabled', applied_zones: [], observed_zones: {}, status: 'disabled' },
  elasticsearch_settings: {
    allocation_enable: 'all', rebalance_enable: 'all', disk_watermark_low: '85%', disk_watermark_high: '90%',
    disk_watermark_flood_stage: '95%', recovery_max_bytes_per_sec: '40mb',
  },
  members: [], assignments: [],
};

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ConsoleContext.Provider value={{ clusters: [cluster], selectedCluster: cluster, selectedClusterId: 1, setSelectedClusterId: () => undefined, watchRun: () => undefined, refreshAll: async () => undefined }}>
        <ClustersPage />
      </ConsoleContext.Provider>
    </QueryClientProvider>,
  );
}

describe('ClustersPage role port associations', () => {
  afterEach(() => {
    cleanup(); state.api.mockClear();
    state.maintenanceCapabilities = {
      planning: false,
      operations: { host_reboot: false, rolling_restart: false, upgrade: false, evacuation: false },
      backends: { documented_rolling: true, node_shutdown: false },
    };
  });

  it('shows distinct suggested role ports and saves the role-port association profile', async () => {
    renderPage();
    fireEvent.click(screen.getByRole('button', { name: 'Create cluster' }));

    expect(screen.queryByLabelText('Desired version')).not.toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Role port associations' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Elasticsearch node ports' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Service listeners' })).toBeInTheDocument();
    expect(screen.getByText('Transport ports are used for Elasticsearch node-to-node communication.')).toBeInTheDocument();
    expect(screen.getByLabelText('Master HTTP port')).toHaveValue(9200);
    expect(screen.getByLabelText('Hot data HTTP port')).toHaveValue(9201);

    fireEvent.change(screen.getByLabelText('Cluster name'), { target: { value: 'lab-b' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save cluster' }));

    await waitFor(() => expect(state.api).toHaveBeenCalledWith('/api/clusters', expect.objectContaining({ method: 'POST' })));
    const request = state.api.mock.calls.find(([path]) => path === '/api/clusters')?.[1];
    const payload = JSON.parse(String(request?.body));
    expect(payload).not.toHaveProperty('desired_version');
    expect(payload.role_ports.master).toEqual({ elasticsearch_http: 9200, elasticsearch_transport: 9300 });
    expect(payload.role_ports.hot).toEqual({ elasticsearch_http: 9201, elasticsearch_transport: 9301 });
  });

  it('blocks a duplicate port assignment before submitting the cluster', async () => {
    renderPage();
    fireEvent.click(screen.getByRole('button', { name: 'Create cluster' }));
    fireEvent.change(screen.getByLabelText('Cluster name'), { target: { value: 'lab-b' } });
    fireEvent.change(screen.getByLabelText('Hot data HTTP port'), { target: { value: '9200' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save cluster' }));

    expect(await screen.findByText('Every role port must be an integer from 1 through 65535 and unique across all roles.')).toBeInTheDocument();
    expect(state.api.mock.calls.some(([path, options]) => path === '/api/clusters' && options?.method === 'POST')).toBe(false);
  });

  it('defines the zone catalog and awareness mode while creating a cluster', async () => {
    renderPage();
    fireEvent.click(screen.getByRole('button', { name: 'Create cluster' }));
    const zoning = screen.getByRole('region', { name: 'Availability zones' });
    expect(within(zoning).getByRole('heading', { name: 'Defined zones' })).toBeInTheDocument();
    expect(within(zoning).getByText('No zones defined.')).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Cluster name'), { target: { value: 'zoned-lab' } });
    fireEvent.change(screen.getByLabelText('Zone awareness mode'), { target: { value: 'awareness' } });
    fireEvent.click(within(zoning).getByRole('button', { name: 'Add zone' }));
    fireEvent.change(screen.getByLabelText('Zone 1'), { target: { value: 'zone-a' } });
    fireEvent.click(within(zoning).getByRole('button', { name: 'Add zone' }));
    fireEvent.change(screen.getByLabelText('Zone 2'), { target: { value: 'zone-b' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save cluster' }));

    await waitFor(() => expect(state.api).toHaveBeenCalledWith('/api/clusters', expect.objectContaining({ method: 'POST' })));
    const request = state.api.mock.calls.find(([path]) => path === '/api/clusters')?.[1];
    expect(JSON.parse(String(request?.body)).zoning).toEqual({ mode: 'awareness', zones: ['zone-a', 'zone-b'] });
  });

  it('starts a tracked zoning apply from the selected cluster editor', async () => {
    state.api.mockImplementation((path) => path === '/api/clusters/1/zoning/apply' ? Promise.resolve({ run_id: 73 }) : Promise.resolve({ available_versions: [], assignments: [] }));
    const watchRun = vi.fn();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <ConsoleContext.Provider value={{ clusters: [cluster], selectedCluster: cluster, selectedClusterId: 1, setSelectedClusterId: () => undefined, watchRun, refreshAll: async () => undefined }}>
          <ClustersPage />
        </ConsoleContext.Provider>
      </QueryClientProvider>,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
    fireEvent.click(screen.getByRole('button', { name: 'Apply zoning' }));
    await waitFor(() => expect(state.api).toHaveBeenCalledWith('/api/clusters/1/zoning/apply', { method: 'POST' }));
    expect(watchRun).toHaveBeenCalledWith(73);
  });

  it('applies the selected Filebeat companion setting as a tracked cluster action', async () => {
    renderPage();
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
    const toggle = screen.getByRole('switch', { name: 'Enable Filebeat companions' });
    expect(toggle).not.toBeChecked();
    fireEvent.click(toggle);
    fireEvent.click(screen.getByRole('button', { name: 'Apply log monitoring' }));

    await waitFor(() => expect(state.api).toHaveBeenCalledWith('/api/clusters/1/log-monitoring', expect.objectContaining({ method: 'PUT' })));
    const request = state.api.mock.calls.find(([path]) => path === '/api/clusters/1/log-monitoring')?.[1];
    expect(JSON.parse(String(request?.body))).toEqual({ filebeat_enabled: true });
  });

  it('shows cluster operations only inside the selected cluster editor', () => {
    renderPage();

    expect(screen.queryByRole('heading', { name: 'Elasticsearch settings' })).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Log monitoring' })).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Versions' })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));

    expect(screen.getByRole('heading', { name: 'Elasticsearch settings' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Log monitoring' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Versions' })).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByRole('heading', { name: 'Elasticsearch settings' })).not.toBeInTheDocument();
  });

  it('shows maintenance policy only for an existing cluster when planning is enabled', async () => {
    state.maintenanceCapabilities.planning = true;
    state.api.mockImplementation((path) => {
      if (path === '/api/clusters/1/maintenance-policy') return Promise.resolve({
        policy: {
          max_unavailable: 1, max_surge: 0, minimum_master_eligible: 'quorum', minimum_data_per_tier: 1,
          minimum_kibana: 1, minimum_fleet_server: 1, minimum_logstash: 1, minimum_coordinating: 1,
          allow_agent_interruption: 'true-with-warning', required_cluster_health: 'green',
          allocation_guard: 'primaries-for-data', observation_max_age_seconds: 120,
          restart_allocation_delay_seconds: null, host_return_timeout_seconds: 900,
          workload_ready_timeout_seconds: 900, plan_validity_seconds: 300,
        },
        revision: 0, customized: false, updated_by: null, updated_at: null,
      });
      if (path === '/api/clusters/1/versions') return Promise.resolve({ available_versions: [], assignments: [] });
      return Promise.resolve({ id: 2 });
    });
    renderPage();

    expect(screen.queryByRole('heading', { name: 'Maintenance policy' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Create cluster' }));
    expect(screen.queryByRole('heading', { name: 'Maintenance policy' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));

    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
    expect(await screen.findByRole('heading', { name: 'Maintenance policy' })).toBeInTheDocument();
    expect(state.api).toHaveBeenCalledWith('/api/clusters/1/maintenance-policy');
  });
});
