import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ConsoleContext } from '../app-context';
import type { Cluster } from '../types';
import { RolesPage } from './RolesPage';

const state = vi.hoisted(() => ({
  api: vi.fn((path: string) => {
    if (path === '/api/health') return Promise.resolve({ roles: [{ id: 'master', label: 'Master' }] });
    if (path === '/api/clusters/1/topology') return Promise.resolve({ topology: '', access_urls: [] });
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

vi.mock('../api', () => ({
  api: state.api,
  jsonBody: vi.fn((value) => ({ body: JSON.stringify(value), headers: { 'Content-Type': 'application/json' } })),
  queries: { nodes: vi.fn().mockResolvedValue([]), runs: vi.fn().mockResolvedValue([]) },
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
  afterEach(() => cleanup());
  it('browses host mounts and fills a dedicated workload path from the selected mount', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <ConsoleContext.Provider value={{ clusters: [cluster], selectedCluster: cluster, selectedClusterId: 1, setSelectedClusterId: () => undefined, watchRun: () => undefined, refreshAll: async () => undefined }}>
          <RolesPage />
        </ConsoleContext.Provider>
      </QueryClientProvider>,
    );

    fireEvent.change(screen.getAllByLabelText('Host')[1], { target: { value: '1' } });
    const mountSelect = await screen.findByLabelText('Host storage mount');
    expect(screen.getByText('/dev/sdb1 · xfs · 1 KiB free of 2 KiB')).toBeInTheDocument();
    expect(screen.getAllByText('selectable')).toHaveLength(2);
    fireEvent.change(mountSelect, { target: { value: '/srv/elastic' } });
    expect(screen.getByLabelText('Storage path')).toHaveValue('/srv/elastic/elastic-control/lab-a/master-1');
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
    fireEvent.change(await screen.findByLabelText('Host storage mount'), { target: { value: '/srv/elastic' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add to pending changes' }));

    expect(await screen.findByRole('heading', { name: 'Pending changes' })).toBeInTheDocument();
    expect(screen.getByText('Create Master')).toBeInTheDocument();
    expect(state.api.mock.calls.some(([path]) => path === '/api/clusters/1/assignments')).toBe(false);

    fireEvent.click(screen.getByRole('button', { name: 'Apply 1 change' }));
    expect(await screen.findByRole('button', { name: 'Apply 1 change' })).toBeDisabled();
    expect(state.api).toHaveBeenCalledWith('/api/clusters/1/workload-changes/apply', expect.objectContaining({ method: 'POST' }));
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
    fireEvent.change(await screen.findByLabelText('Host storage mount'), { target: { value: '/srv/elastic' } });
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
    fireEvent.change(await screen.findByLabelText('Host storage mount'), { target: { value: '/srv/elastic' } });
    fireEvent.click(screen.getByRole('button', { name: 'Add to pending changes' }));
    await screen.findByRole('heading', { name: 'Pending changes' });

    const proceed = vi.fn();
    expect(navigationGuard?.(proceed)).toBe(true);
    expect(await screen.findByRole('heading', { name: 'Discard pending workload changes?' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Discard changes' }));
    expect(proceed).toHaveBeenCalledOnce();
  });
});
