import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ConsoleContext } from '../app-context';
import type { Cluster } from '../features/clusters';
import type { DashboardSnapshot } from '../features/dashboard';
import { DashboardPage } from './DashboardPage';

const state = vi.hoisted(() => {
  const initialData: DashboardSnapshot = {
  generated_at: '2026-08-01T10:00:00Z',
  clusters: [{
    id: 1, name: 'Production', slug: 'production', theme_color: '#0077CC', health: 'green', node_count: 3, workload_count: 3,
    metrics: {
      cluster_id: 1, status: 'green', nodes: 3, data_nodes: 2, active_shards: 12, unassigned_shards: 0, heap_used_bytes: 600, heap_max_bytes: 1800,
      node_breakdown: [
        { id: 'hot-id', name: 'hot-1', node_type: 'Hot data', roles: ['master', 'data_hot'], zone: 'zone-a', shards: 7, disk_used_bytes: 600, disk_total_bytes: 1000, disk_available_bytes: 400, heap_used_bytes: 300, heap_max_bytes: 600 },
        { id: 'warm-id', name: 'warm-1', node_type: 'Warm data', roles: ['data_warm'], zone: 'zone-b', shards: 4, disk_used_bytes: 800, disk_total_bytes: 2000, disk_available_bytes: 1200, heap_used_bytes: 200, heap_max_bytes: 800 },
        { id: 'other-id', name: 'ingest-1', node_type: 'Ingest', roles: ['ingest'], zone: '', shards: 0, disk_used_bytes: 200, disk_total_bytes: 500, disk_available_bytes: 300, heap_used_bytes: 100, heap_max_bytes: 400 },
      ],
      zone_breakdown: [
        { zone: 'zone-a', nodes: 1, shards: 7, disk_used_bytes: 600, disk_total_bytes: 1000, disk_available_bytes: 400, heap_used_bytes: 300, heap_max_bytes: 600 },
        { zone: 'zone-b', nodes: 1, shards: 4, disk_used_bytes: 800, disk_total_bytes: 2000, disk_available_bytes: 1200, heap_used_bytes: 200, heap_max_bytes: 800 },
      ],
    },
    history: [],
  }],
  hosts: [],
  alerts: [],
  };
  return { data: initialData, initialData };
});

vi.mock('../features/dashboard', () => ({
  dashboardApi: {
    snapshot: vi.fn(() => Promise.resolve(state.data)),
    controllerSettings: vi.fn(() => Promise.resolve({ timezone: 'UTC' })),
    topology: vi.fn(() => Promise.resolve({ topology: '', access_urls: [{ assignment_id: 10, role: 'kibana', label: 'Kibana', audience: 'browser', url: 'https://192.0.2.102:5601' }] })),
    streamToken: vi.fn(() => Promise.resolve({ token: 'dashboard-stream-token' })),
  },
}));

vi.mock('echarts-for-react', () => ({ default: () => <div data-testid="echarts-chart" /> }));

class EventSourceMock {
  addEventListener() {}
  close() {}
}

describe('DashboardPage', () => {
  beforeEach(() => vi.stubGlobal('EventSource', EventSourceMock));
  afterEach(() => { cleanup(); state.data = state.initialData; vi.unstubAllGlobals(); });

  it('shows a neutral setup state for a cluster with no configured hosts or workloads', async () => {
    state.data = {
      ...state.data,
      clusters: [{
        ...state.data.clusters[0],
        health: 'unknown',
        node_count: 0,
        workload_count: 0,
        metrics: { cluster_id: 1, status: 'unknown', last_error: 'No master is assigned' },
        history: [],
      }],
      alerts: [{ severity: 'warning', source: 'cluster', source_id: 1, message: 'No master is assigned' }],
    };
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const cluster: Cluster = {
      id: 1, name: 'Production', slug: 'production', theme_color: '#0077CC', desired_version: '8.19.0',
      ports: { elasticsearch_http: 9200, elasticsearch_transport: 9300, kibana: 5601, fleet: 8220, logstash_api: 9600 },
      network_defaults: { mode: 'shared' as const },
      elasticsearch_settings: {
        allocation_enable: 'all', rebalance_enable: 'all', disk_watermark_low: '85%', disk_watermark_high: '90%',
        disk_watermark_flood_stage: '95%', recovery_max_bytes_per_sec: '40mb',
      },
      members: [], assignments: [],
    };
    render(
      <QueryClientProvider client={client}>
        <ConsoleContext.Provider value={{ clusters: [cluster], selectedCluster: cluster, selectedClusterId: 1, setSelectedClusterId: () => undefined, watchRun: () => undefined, refreshAll: async () => undefined }}>
          <DashboardPage />
        </ConsoleContext.Provider>
      </QueryClientProvider>,
    );

    expect(await screen.findByText('Cluster setup not started')).toBeInTheDocument();
    expect(screen.getByText('Setup needed')).toBeInTheDocument();
    expect(screen.queryByText('No master is assigned')).not.toBeInTheDocument();
    expect(screen.queryByText('Metrics degraded')).not.toBeInTheDocument();
    expect(screen.queryByText('Elasticsearch nodes')).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Configure roles' })).toHaveAttribute('href', '/roles');
  });

  it('explains that a master-only cluster is awaiting a data role', async () => {
    state.data = {
      ...state.data,
      clusters: [{
        ...state.data.clusters[0],
        health: 'awaiting_data',
        node_count: 1,
        workload_count: 1,
        metrics: { cluster_id: 1, status: 'awaiting_data', nodes: 1, data_nodes: 0 },
        history: [],
      }],
      alerts: [],
    };
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const cluster: Cluster = {
      id: 1, name: 'Production', slug: 'production', theme_color: '#0077CC', desired_version: '8.19.0',
      ports: { elasticsearch_http: 9200, elasticsearch_transport: 9300, kibana: 5601, fleet: 8220, logstash_api: 9600 },
      network_defaults: { mode: 'shared' as const },
      elasticsearch_settings: {
        allocation_enable: 'all', rebalance_enable: 'all', disk_watermark_low: '85%', disk_watermark_high: '90%',
        disk_watermark_flood_stage: '95%', recovery_max_bytes_per_sec: '40mb',
      },
      members: [], assignments: [],
    };
    render(
      <QueryClientProvider client={client}>
        <ConsoleContext.Provider value={{ clusters: [cluster], selectedCluster: cluster, selectedClusterId: 1, setSelectedClusterId: () => undefined, watchRun: () => undefined, refreshAll: async () => undefined }}>
          <DashboardPage />
        </ConsoleContext.Provider>
      </QueryClientProvider>,
    );

    expect(await screen.findByText('Data role required')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Assign data role' })).toHaveAttribute('href', '/roles');
    expect(screen.queryByText('Elasticsearch nodes')).not.toBeInTheDocument();
    expect((await screen.findAllByLabelText('Awaiting data role status: A master is running, but a Hot data or Warm data role is required before Elasticsearch can allocate data and report health.')).length).toBeGreaterThan(0);
  });

  it('lists selected-cluster user access endpoints', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const cluster: Cluster = {
      id: 1, name: 'Production', slug: 'production', theme_color: '#0077CC', desired_version: '8.19.0',
      ports: { elasticsearch_http: 9200, elasticsearch_transport: 9300, kibana: 5601, fleet: 8220, logstash_api: 9600 },
      network_defaults: { mode: 'shared' as const },
      elasticsearch_settings: {
        allocation_enable: 'all', rebalance_enable: 'all', disk_watermark_low: '85%', disk_watermark_high: '90%',
        disk_watermark_flood_stage: '95%', recovery_max_bytes_per_sec: '40mb',
      },
      members: [], assignments: [],
    };
    render(
      <QueryClientProvider client={client}>
        <ConsoleContext.Provider value={{ clusters: [cluster], selectedCluster: cluster, selectedClusterId: 1, setSelectedClusterId: () => undefined, watchRun: () => undefined, refreshAll: async () => undefined }}>
          <DashboardPage />
        </ConsoleContext.Provider>
      </QueryClientProvider>,
    );

    expect(await screen.findByRole('heading', { name: 'User access endpoints' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /https:\/\/192\.0\.2\.102:5601/ })).toHaveAttribute('href', 'https://192.0.2.102:5601');
  });

  it('opens Kibana Discover with the selected cluster log data-stream filter', async () => {
    state.data = {
      ...state.data,
      clusters: [{ ...state.data.clusters[0], log_monitoring: { filebeat_enabled: true, retention_days: 30, companion_state: 'running' } }],
    };
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const cluster: Cluster = {
      id: 1, name: 'Production', slug: 'production', theme_color: '#0077CC', desired_version: '8.19.0',
      ports: { elasticsearch_http: 9200, elasticsearch_transport: 9300, kibana: 5601, fleet: 8220, logstash_api: 9600 },
      network_defaults: { mode: 'shared' as const }, log_monitoring: { filebeat_enabled: true, retention_days: 30, companion_state: 'running' },
      elasticsearch_settings: {
        allocation_enable: 'all', rebalance_enable: 'all', disk_watermark_low: '85%', disk_watermark_high: '90%',
        disk_watermark_flood_stage: '95%', recovery_max_bytes_per_sec: '40mb',
      },
      members: [], assignments: [],
    };
    render(
      <QueryClientProvider client={client}>
        <ConsoleContext.Provider value={{ clusters: [cluster], selectedCluster: cluster, selectedClusterId: 1, setSelectedClusterId: () => undefined, watchRun: () => undefined, refreshAll: async () => undefined }}>
          <DashboardPage />
        </ConsoleContext.Provider>
      </QueryClientProvider>,
    );

    const link = await screen.findByRole('link', { name: 'Open Kibana logs' });
    expect(link).toHaveAttribute('href', expect.stringContaining('/app/discover#/'));
    expect(link.getAttribute('href')).toContain("dataViewId:'elkeeper-logs-production'");
    expect(link.getAttribute('href')).toContain('data_stream.dataset');
    expect(link.getAttribute('href')).toContain('elkeeper.production');
  });

  it('shows data-tier disk capacity alongside all-node JVM heap and reveals scoped details on demand', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const cluster: Cluster = {
      id: 1, name: 'Production', slug: 'production', theme_color: '#0077CC', desired_version: '8.19.0',
      ports: { elasticsearch_http: 9200, elasticsearch_transport: 9300, kibana: 5601, fleet: 8220, logstash_api: 9600 },
      network_defaults: { mode: 'shared' as const },
      elasticsearch_settings: {
        allocation_enable: 'all', rebalance_enable: 'all', disk_watermark_low: '85%', disk_watermark_high: '90%',
        disk_watermark_flood_stage: '95%', recovery_max_bytes_per_sec: '40mb',
      },
      members: [], assignments: [],
    };
    render(
      <QueryClientProvider client={client}>
        <ConsoleContext.Provider value={{ clusters: [cluster], selectedCluster: cluster, selectedClusterId: 1, setSelectedClusterId: () => undefined, watchRun: () => undefined, refreshAll: async () => undefined }}>
          <DashboardPage />
        </ConsoleContext.Provider>
      </QueryClientProvider>,
    );

    const button = await screen.findByRole('button', { name: 'Show node details' });
    expect(screen.getByText('Data-tier disk used')).toBeInTheDocument();
    expect(screen.getByText(/^2 data-tier nodes\./)).toBeInTheDocument();
    expect(screen.getByText('1 KiB / 3 KiB')).toBeInTheDocument();
    expect(screen.getByText('All Elasticsearch JVM heap used')).toBeInTheDocument();
    expect(screen.getByText(/^3 nodes\. JVM heap, not host RAM\./)).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Capacity and shard details' })).toBeInTheDocument();
    expect(screen.queryByText('Hot data')).not.toBeInTheDocument();
    fireEvent.click(button);
    expect(screen.getAllByText('Hot data').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Warm data').length).toBeGreaterThan(0);
    expect(screen.getByText('Ingest')).toBeInTheDocument();
    expect(screen.getAllByText('Zone').length).toBeGreaterThan(0);
    expect(screen.getAllByText('zone-a').length).toBeGreaterThan(0);
    expect(screen.getByRole('heading', { name: 'Data-tier capacity and shard breakdown' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Data-tier zone capacity and shard distribution' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'All Elasticsearch node diagnostics' })).toBeInTheDocument();
    expect(screen.getAllByText('7').length).toBeGreaterThan(0);
  });

  it('includes every Elasticsearch data role exactly once in data-tier capacity and excludes non-data roles', async () => {
    state.data = {
      ...state.data,
      clusters: [{
        ...state.data.clusters[0],
        node_count: 7,
        metrics: {
          ...state.data.clusters[0].metrics,
          nodes: 7,
          heap_used_bytes: 1_050,
          heap_max_bytes: 2_100,
          node_breakdown: [
            { id: 'hot-id', name: 'hot-1', node_type: 'Hot data', roles: ['master', 'data_hot'], zone: 'zone-a', shards: 1, disk_used_bytes: 100, disk_total_bytes: 1000, disk_available_bytes: 900, heap_used_bytes: 100, heap_max_bytes: 200 },
            { id: 'warm-id', name: 'warm-1', node_type: 'Warm data', roles: ['data_warm'], zone: 'zone-b', shards: 1, disk_used_bytes: 100, disk_total_bytes: 1000, disk_available_bytes: 900, heap_used_bytes: 100, heap_max_bytes: 200 },
            { id: 'cold-id', name: 'cold-1', node_type: 'Cold data', roles: ['data_cold'], zone: 'zone-a', shards: 1, disk_used_bytes: 100, disk_total_bytes: 1000, disk_available_bytes: 900, heap_used_bytes: 100, heap_max_bytes: 200 },
            { id: 'frozen-id', name: 'frozen-1', node_type: 'Frozen data', roles: ['data_frozen'], zone: 'zone-b', shards: 1, disk_used_bytes: 100, disk_total_bytes: 1000, disk_available_bytes: 900, heap_used_bytes: 100, heap_max_bytes: 200 },
            { id: 'content-id', name: 'content-1', node_type: 'Content data', roles: ['data_content'], zone: 'zone-a', shards: 1, disk_used_bytes: 100, disk_total_bytes: 1000, disk_available_bytes: 900, heap_used_bytes: 100, heap_max_bytes: 200 },
            { id: 'data-id', name: 'data-1', node_type: 'Data', roles: ['data'], zone: 'zone-b', shards: 1, disk_used_bytes: 100, disk_total_bytes: 1000, disk_available_bytes: 900, heap_used_bytes: 100, heap_max_bytes: 200 },
            { id: 'ingest-id', name: 'ingest-1', node_type: 'Ingest', roles: ['ingest'], zone: 'zone-c', shards: 0, disk_used_bytes: 900, disk_total_bytes: 9000, disk_available_bytes: 8100, heap_used_bytes: 450, heap_max_bytes: 900 },
          ],
        },
      }],
    };
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const cluster: Cluster = {
      id: 1, name: 'Production', slug: 'production', theme_color: '#0077CC', desired_version: '8.19.0',
      ports: { elasticsearch_http: 9200, elasticsearch_transport: 9300, kibana: 5601, fleet: 8220, logstash_api: 9600 },
      network_defaults: { mode: 'shared' as const },
      elasticsearch_settings: {
        allocation_enable: 'all', rebalance_enable: 'all', disk_watermark_low: '85%', disk_watermark_high: '90%',
        disk_watermark_flood_stage: '95%', recovery_max_bytes_per_sec: '40mb',
      },
      members: [], assignments: [],
    };
    render(
      <QueryClientProvider client={client}>
        <ConsoleContext.Provider value={{ clusters: [cluster], selectedCluster: cluster, selectedClusterId: 1, setSelectedClusterId: () => undefined, watchRun: () => undefined, refreshAll: async () => undefined }}>
          <DashboardPage />
        </ConsoleContext.Provider>
      </QueryClientProvider>,
    );

    expect(await screen.findByText(/^6 data-tier nodes\./)).toBeInTheDocument();
    expect(screen.getByText('600 B / 6 KiB')).toBeInTheDocument();
    expect(screen.getByText(/^7 nodes\. JVM heap, not host RAM\./)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Show node details' }));
    expect(screen.getByRole('heading', { name: 'Data-tier capacity and shard breakdown' })).toBeInTheDocument();
    expect(screen.getAllByText('Cold data')).toHaveLength(2);
    expect(screen.getAllByText('Frozen data')).toHaveLength(2);
    expect(screen.getAllByText('Content data')).toHaveLength(2);
    expect(screen.getAllByText('Ingest')).toHaveLength(1);
    expect(screen.getByRole('heading', { name: 'Data-tier zone capacity and shard distribution' })).toBeInTheDocument();
  });

  it('marks data-tier disk capacity unavailable when per-node telemetry is absent', async () => {
    state.data = {
      ...state.data,
      clusters: [{
        ...state.data.clusters[0],
        metrics: { cluster_id: 1, status: 'green', nodes: 2, heap_used_bytes: 500, heap_max_bytes: 1000 },
      }],
    };
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const cluster: Cluster = {
      id: 1, name: 'Production', slug: 'production', theme_color: '#0077CC', desired_version: '8.19.0',
      ports: { elasticsearch_http: 9200, elasticsearch_transport: 9300, kibana: 5601, fleet: 8220, logstash_api: 9600 },
      network_defaults: { mode: 'shared' as const },
      elasticsearch_settings: {
        allocation_enable: 'all', rebalance_enable: 'all', disk_watermark_low: '85%', disk_watermark_high: '90%',
        disk_watermark_flood_stage: '95%', recovery_max_bytes_per_sec: '40mb',
      },
      members: [], assignments: [],
    };
    render(
      <QueryClientProvider client={client}>
        <ConsoleContext.Provider value={{ clusters: [cluster], selectedCluster: cluster, selectedClusterId: 1, setSelectedClusterId: () => undefined, watchRun: () => undefined, refreshAll: async () => undefined }}>
          <DashboardPage />
        </ConsoleContext.Provider>
      </QueryClientProvider>,
    );

    expect(await screen.findByText('Data-tier disk capacity unavailable')).toBeInTheDocument();
    expect(screen.getByText(/^2 nodes\. JVM heap, not host RAM\./)).toBeInTheDocument();
    expect(screen.queryByText(/NaN/)).not.toBeInTheDocument();
  });

  it('explains the selected cluster health status on hover and focus', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const cluster: Cluster = {
      id: 1, name: 'Production', slug: 'production', theme_color: '#0077CC', desired_version: '8.19.0',
      ports: { elasticsearch_http: 9200, elasticsearch_transport: 9300, kibana: 5601, fleet: 8220, logstash_api: 9600 },
      network_defaults: { mode: 'shared' as const },
      elasticsearch_settings: {
        allocation_enable: 'all', rebalance_enable: 'all', disk_watermark_low: '85%', disk_watermark_high: '90%',
        disk_watermark_flood_stage: '95%', recovery_max_bytes_per_sec: '40mb',
      },
      members: [], assignments: [],
    };
    render(
      <QueryClientProvider client={client}>
        <ConsoleContext.Provider value={{ clusters: [cluster], selectedCluster: cluster, selectedClusterId: 1, setSelectedClusterId: () => undefined, watchRun: () => undefined, refreshAll: async () => undefined }}>
          <DashboardPage />
        </ConsoleContext.Provider>
      </QueryClientProvider>,
    );

    const [health] = await screen.findAllByLabelText('Green status: All primary and replica shards are assigned and configured hosts are reachable.');
    fireEvent.mouseOver(health);
    expect(await screen.findByText('All primary and replica shards are assigned and configured hosts are reachable.')).toBeInTheDocument();
  });

  it('renders cross-cluster host resource graphs with cluster attribution', async () => {
    state.data = {
      ...state.data,
      cross_cluster_host_usage: [{
        node_id: 7,
        name: 'node-a',
        reachable: true,
        observed_at: '2026-08-01T10:00:00Z',
        last_error: '',
        resource_observation_error: '',
        clusters: [{ id: 1, name: 'Production', theme_color: '#0077CC' }],
        history: [{
          observed_at: '2026-08-01T10:00:00Z',
          cpu_percent: 42.5,
          memory_usage_bytes: 6_000,
          memory_total_bytes: 8_000,
          network_rx_bytes_per_second: 100,
          network_tx_bytes_per_second: 250,
          disk_read_bytes_per_second: 150,
          disk_write_bytes_per_second: 300,
        }],
      }],
    };
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const cluster: Cluster = {
      id: 1, name: 'Production', slug: 'production', theme_color: '#0077CC', desired_version: '8.19.0',
      ports: { elasticsearch_http: 9200, elasticsearch_transport: 9300, kibana: 5601, fleet: 8220, logstash_api: 9600 },
      network_defaults: { mode: 'shared' as const },
      elasticsearch_settings: {
        allocation_enable: 'all', rebalance_enable: 'all', disk_watermark_low: '85%', disk_watermark_high: '90%',
        disk_watermark_flood_stage: '95%', recovery_max_bytes_per_sec: '40mb',
      },
      members: [], assignments: [],
    };
    render(
      <QueryClientProvider client={client}>
        <ConsoleContext.Provider value={{ clusters: [cluster], selectedCluster: cluster, selectedClusterId: 1, setSelectedClusterId: () => undefined, watchRun: () => undefined, refreshAll: async () => undefined }}>
          <DashboardPage />
        </ConsoleContext.Provider>
      </QueryClientProvider>,
    );

    expect(await screen.findByRole('heading', { name: 'Cross-cluster host resource usage' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'node-a' })).toBeInTheDocument();
    expect(screen.getAllByText('Production').length).toBeGreaterThan(2);
    expect(screen.getByLabelText('Network bandwidth for node-a')).toBeInTheDocument();
    expect(screen.getByLabelText('Disk I/O for node-a')).toBeInTheDocument();
  });
});
