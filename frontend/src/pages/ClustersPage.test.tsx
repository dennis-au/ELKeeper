import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ConsoleContext } from '../app-context';
import type { Cluster } from '../types';
import { ClustersPage } from './ClustersPage';

const state = vi.hoisted(() => ({
  api: vi.fn<(path: string, options?: RequestInit) => Promise<unknown>>((path) => {
    if (path === '/api/clusters/1/versions') return Promise.resolve({ available_versions: [], assignments: [] });
    if (path === '/api/clusters/1/log-monitoring') return Promise.resolve({ run_id: 55 });
    return Promise.resolve({ id: 2 });
  }),
}));

vi.mock('../api', () => ({
  api: state.api,
  jsonBody: vi.fn((value) => ({ body: JSON.stringify(value), headers: { 'Content-Type': 'application/json' } })),
}));

const cluster: Cluster = {
  id: 1, name: 'lab-a', slug: 'lab-a', theme_color: '#0077CC', desired_version: '8.19.0',
  ports: { elasticsearch_http: 9200, elasticsearch_transport: 9300, kibana: 5601, fleet: 8220, logstash_api: 9600 },
  network_defaults: { mode: 'shared' },
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
  afterEach(() => { cleanup(); state.api.mockClear(); });

  it('shows distinct suggested role ports and saves the role-port association profile', async () => {
    renderPage();
    fireEvent.click(screen.getByRole('button', { name: 'Create cluster' }));

    expect(screen.getByRole('heading', { name: 'Role port associations' })).toBeInTheDocument();
    expect(screen.getByLabelText('Master HTTP port')).toHaveValue(9200);
    expect(screen.getByLabelText('Hot data HTTP port')).toHaveValue(9201);

    fireEvent.change(screen.getByLabelText('Cluster name'), { target: { value: 'lab-b' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save cluster' }));

    await waitFor(() => expect(state.api).toHaveBeenCalledWith('/api/clusters', expect.objectContaining({ method: 'POST' })));
    const request = state.api.mock.calls.find(([path]) => path === '/api/clusters')?.[1];
    const payload = JSON.parse(String(request?.body));
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

  it('applies the selected Filebeat companion setting as a tracked cluster action', async () => {
    renderPage();
    const toggle = screen.getByRole('switch', { name: 'Enable Filebeat companions' });
    expect(toggle).not.toBeChecked();
    fireEvent.click(toggle);
    fireEvent.click(screen.getByRole('button', { name: 'Apply log monitoring' }));

    await waitFor(() => expect(state.api).toHaveBeenCalledWith('/api/clusters/1/log-monitoring', expect.objectContaining({ method: 'PUT' })));
    const request = state.api.mock.calls.find(([path]) => path === '/api/clusters/1/log-monitoring')?.[1];
    expect(JSON.parse(String(request?.body))).toEqual({ filebeat_enabled: true });
  });
});
