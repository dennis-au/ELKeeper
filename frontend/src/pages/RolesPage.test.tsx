import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ConsoleContext } from '../app-context';
import type { Cluster } from '../features/clusters';
import { managedWorkloadColumns, RolesPage, workloadImageVersion, workloadRuntimeVersionLabel } from './RolesPage';

const state = vi.hoisted(() => ({
  versionResponse: { available_versions: ['8.20.0', '8.19.0'], recommended_version: '8.20.0', assignments: [] },
  api: vi.fn((path: string, _options?: RequestInit) => {
    if (path === '/api/health') return Promise.resolve({ roles: [{ id: 'master', label: 'Master' }] });
    if (path === '/api/clusters/1/topology') return Promise.resolve({ topology: '', access_urls: [] });
    if (path === '/api/clusters/1/versions?role=master') return Promise.resolve(state.versionResponse);
    if (path === '/api/runs') return Promise.resolve([]);
    if (path === '/api/clusters/1/workload-changes/apply') return Promise.resolve({ run_id: 77 });
    if (path === '/api/nodes/1/storage') return Promise.resolve({
      node_id: 1,
      observed_at: '2026-08-01T00:00:00Z',
      mounts: [
        { mount_point: '/', source: '/dev/mapper/root', filesystem: 'xfs', size_bytes: 1000, available_bytes: 600, writable: true, eligible: true, unavailable_reason: '' },
        { mount_point: '/srv/elastic', source: '/dev/sdb1', filesystem: 'xfs', size_bytes: 2000, available_bytes: 1500, writable: true, eligible: true, unavailable_reason: '' },
      ],
    });
    return Promise.resolve({});
  }),
}));

vi.mock('../features/versions', () => ({
  versionsApi: { list: vi.fn(() => Promise.resolve(state.versionResponse)) },
}));

vi.mock('../features/workloads', () => ({
  workloadsApi: {
    storage: (nodeId: number) => state.api(`/api/nodes/${nodeId}/storage`),
    removeAssignment: (assignmentId: number, mode: string) => state.api(`/api/assignments/${assignmentId}?mode=${mode}`, { method: 'DELETE' }),
    applyChanges: (clusterId: number, payload: unknown) => state.api(`/api/clusters/${clusterId}/workload-changes/apply`, { method: 'POST', body: JSON.stringify(payload) }),
    topology: (clusterId: number) => state.api(`/api/clusters/${clusterId}/topology`),
    roles: () => state.api('/api/health'),
  },
}));

vi.mock('../features/hosts', () => ({
  hostApi: { list: () => Promise.resolve([]) },
}));

vi.mock('../features/clusters', () => ({
  clusterApi: {
    addMember: (clusterId: number, input: unknown) => state.api(`/api/clusters/${clusterId}/members`, { method: 'POST', body: JSON.stringify(input) }),
    updateMember: (clusterId: number, nodeId: number, input: unknown) => state.api(`/api/clusters/${clusterId}/members/${nodeId}`, { method: 'PUT', body: JSON.stringify(input) }),
    removeMember: (clusterId: number, nodeId: number) => state.api(`/api/clusters/${clusterId}/members/${nodeId}`, { method: 'DELETE' }),
  },
}));

vi.mock('../features/runs', () => ({
  runsApi: { list: () => state.api('/api/runs') },
}));

const cluster: Cluster = {
  id: 1, name: 'lab-a', slug: 'lab-a', theme_color: '#0077CC', desired_version: '8.19.0',
  ports: { elasticsearch_http: 9200, elasticsearch_transport: 9300, kibana: 5601, fleet: 8220, logstash_api: 9600 },
  network_defaults: { mode: 'shared' },
  elasticsearch_settings: {
    allocation_enable: 'all', rebalance_enable: 'all', disk_watermark_low: '85%', disk_watermark_high: '90%',
    disk_watermark_flood_stage: '95%', recovery_max_bytes_per_sec: '40mb',
  },
  members: [{ cluster_id: 1, node_id: 1, name: 'node-a', address: '192.0.2.102', enabled: true, network_mode: 'shared', data_interface: 'ens18', data_address: '192.0.2.102', user_interface: 'ens18', user_address: '192.0.2.102', network_ready: true }],
  assignments: [],
};

describe('RolesPage storage selection', () => {
  afterEach(() => {
    cleanup();
    state.versionResponse = { available_versions: ['8.20.0', '8.19.0'], recommended_version: '8.20.0', assignments: [] };
  });
  it('browses host mounts and fills a dedicated workload path from the selected mount', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <ConsoleContext.Provider value={{ clusters: [cluster], selectedCluster: cluster, selectedClusterId: 1, setSelectedClusterId: () => undefined, watchRun: () => undefined, refreshAll: async () => undefined }}>
          <RolesPage />
        </ConsoleContext.Provider>
      </QueryClientProvider>,
    );

    expect(screen.getByRole('heading', { name: 'Placement' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Resources' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Storage' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Advanced configuration' })).toBeInTheDocument();
    expect(screen.getByText('Host required')).toBeInTheDocument();
    fireEvent.change(screen.getAllByLabelText('Host')[1], { target: { value: '1' } });
    await waitFor(() => expect(screen.getByText('Storage required')).toBeInTheDocument());
    const mountSelect = await screen.findByLabelText('Host storage mount');
    expect(screen.getByText('/dev/sdb1 · xfs · 1 KiB free of 2 KiB')).toBeInTheDocument();
    expect(screen.getAllByText('selectable')).toHaveLength(2);
    fireEvent.change(mountSelect, { target: { value: '/srv/elastic' } });
    expect(screen.getByLabelText('Storage path')).toHaveValue('/srv/elastic/elastic-control/lab-a/master-1');
    await waitFor(() => expect(screen.getByText('Ready to stage')).toBeInTheDocument());
    expect(state.api).toHaveBeenCalledWith('/api/nodes/1/storage');
  });

  it('stages a workload locally and submits the complete pending change set only when applied', async () => {
    state.api.mockClear();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <ConsoleContext.Provider value={{ clusters: [cluster], selectedCluster: cluster, selectedClusterId: 1, setSelectedClusterId: () => undefined, watchRun: () => undefined, refreshAll: async () => undefined }}>
          <RolesPage />
        </ConsoleContext.Provider>
      </QueryClientProvider>,
    );

    fireEvent.change(screen.getAllByLabelText('Host')[1], { target: { value: '1' } });
    expect(await screen.findByLabelText('Image version')).toHaveValue('8.20.0');
    fireEvent.change(await screen.findByLabelText('Host storage mount'), { target: { value: '/srv/elastic' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add to pending changes' }));

    expect(await screen.findByRole('heading', { name: 'Pending changes' })).toBeInTheDocument();
    expect(screen.getByText('Create Master')).toBeInTheDocument();
    expect(state.api.mock.calls.some(([path]) => path === '/api/clusters/1/assignments')).toBe(false);

    fireEvent.click(screen.getByRole('button', { name: 'Apply 1 change' }));
    expect(await screen.findByRole('button', { name: 'Apply 1 change' })).toBeDisabled();
    expect(state.api).toHaveBeenCalledWith('/api/clusters/1/workload-changes/apply', expect.objectContaining({ method: 'POST' }));
    const request = state.api.mock.calls.find(([path]) => path === '/api/clusters/1/workload-changes/apply')?.[1];
    expect(JSON.parse(String(request?.body)).changes[0].image_version).toBe('8.20.0');
  });

  it('defaults staged workloads to the current cluster version when one is recommended', async () => {
    state.versionResponse = { available_versions: ['8.20.0', '8.19.0'], recommended_version: '8.19.0', assignments: [] };
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <ConsoleContext.Provider value={{ clusters: [cluster], selectedCluster: cluster, selectedClusterId: 1, setSelectedClusterId: () => undefined, watchRun: () => undefined, refreshAll: async () => undefined }}>
          <RolesPage />
        </ConsoleContext.Provider>
      </QueryClientProvider>,
    );

    await waitFor(() => expect(screen.getByLabelText('Image version')).toHaveValue('8.19.0'));
  });

  it('shows the observed running image version immediately after the workload role', async () => {
    const clusterWithAssignment: Cluster = {
      ...cluster,
      assignments: [{
        id: 9, cluster_id: 1, node_id: 1, node_name: 'node-a', role: 'master', state: 'active', revision: 1,
        image_version: '8.19.0', config: { cpu: '2', memory: '4g', storage_path: '/srv/elastic/master-1' },
        observation: {
          image: 'docker.elastic.co/elasticsearch/elasticsearch:8.19.1', digest: 'sha256:test', version: '8.19.1',
          running: true, cached: true, observed_at: '2026-08-02T00:00:00Z', error: '',
        },
      }],
    };
    expect(managedWorkloadColumns.slice(0, 2).map((column) => column.id)).toEqual(['role', 'version']);
    expect(managedWorkloadColumns[1].display).toBe('Image version');
    expect(workloadImageVersion(clusterWithAssignment.assignments[0])).toBe('8.19.1');
    expect(workloadImageVersion({ ...clusterWithAssignment.assignments[0], image_version: '8.19.0', observation: undefined })).toBe('not observed');
    expect(workloadRuntimeVersionLabel(clusterWithAssignment.assignments[0])).toBe('8.19.1');
    expect(workloadRuntimeVersionLabel({ ...clusterWithAssignment.assignments[0], image_version: '8.19.0', observation: undefined })).toBe('Version not observed');

    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <ConsoleContext.Provider value={{ clusters: [clusterWithAssignment], selectedCluster: clusterWithAssignment, selectedClusterId: 1, setSelectedClusterId: () => undefined, watchRun: () => undefined, refreshAll: async () => undefined }}>
          <RolesPage />
        </ConsoleContext.Provider>
      </QueryClientProvider>,
    );
    const matrix = await screen.findByLabelText('Cluster workload placement matrix');
    expect(within(matrix).getByText('8.19.1')).toBeInTheDocument();
  });

  it('keeps a staged workload editable until the complete change set is applied', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <ConsoleContext.Provider value={{ clusters: [cluster], selectedCluster: cluster, selectedClusterId: 1, setSelectedClusterId: () => undefined, watchRun: () => undefined, refreshAll: async () => undefined }}>
          <RolesPage />
        </ConsoleContext.Provider>
      </QueryClientProvider>,
    );

    fireEvent.change(screen.getAllByLabelText('Host')[1], { target: { value: '1' } });
    await waitFor(() => expect(screen.getByLabelText('Image version')).toHaveValue('8.20.0'));
    fireEvent.change(await screen.findByLabelText('Host storage mount'), { target: { value: '/srv/elastic' } });
    await waitFor(() => expect(screen.getByRole('button', { name: 'Add to pending changes' })).not.toBeDisabled());
    fireEvent.click(screen.getByRole('button', { name: 'Add to pending changes' }));

    fireEvent.click(await screen.findByRole('button', { name: 'Edit pending workload master on node-a' }));
    expect(screen.getByRole('button', { name: 'Update pending change' })).toBeInTheDocument();
  });

  it('warns in an in-page dialog before discarding unapplied changes for navigation', async () => {
    let navigationGuard: ((proceed: () => void) => boolean) | undefined;
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <ConsoleContext.Provider value={{
          clusters: [cluster], selectedCluster: cluster, selectedClusterId: 1,
          setSelectedClusterId: () => undefined, watchRun: () => undefined, refreshAll: async () => undefined,
          registerNavigationGuard: (guard) => { navigationGuard = guard; },
        }}>
          <RolesPage />
        </ConsoleContext.Provider>
      </QueryClientProvider>,
    );

    fireEvent.change(screen.getAllByLabelText('Host')[1], { target: { value: '1' } });
    await waitFor(() => expect(screen.getByLabelText('Image version')).toHaveValue('8.20.0'));
    fireEvent.change(await screen.findByLabelText('Host storage mount'), { target: { value: '/srv/elastic' } });
    await waitFor(() => expect(screen.getByRole('button', { name: 'Add to pending changes' })).not.toBeDisabled());
    fireEvent.click(screen.getByRole('button', { name: 'Add to pending changes' }));
    await screen.findByRole('heading', { name: 'Pending changes' });

    const proceed = vi.fn();
    expect(navigationGuard?.(proceed)).toBe(true);
    expect(await screen.findByRole('heading', { name: 'Discard pending workload changes?' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Discard changes' }));
    expect(proceed).toHaveBeenCalledOnce();
  });
});
