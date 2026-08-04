import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ConsoleContext } from '../app-context';
import type { Cluster } from '../features/clusters';
import { AdvancedPage } from './AdvancedPage';

const state = vi.hoisted(() => ({
  items: [
    { id: 'cluster.elastic_password', label: 'Elastic superuser password', category: 'Credentials', source: 'controller', available: true, masked_value: '********' },
    { id: 'cluster.ca_certificate', label: 'Cluster CA certificate', category: 'Certificates', source: 'node-a', available: true, masked_value: '********', storage_path: '/etc/elastic-control/clusters/lab-a/ca/ca.crt' },
    { id: 'cluster.ca_private_key', label: 'Cluster CA private key', category: 'Private keys', source: 'node-a', available: true, masked_value: '********', storage_path: '/etc/elastic-control/clusters/lab-a/ca/ca.key' },
  ],
  request: vi.fn(),
}));

vi.mock('../shared/api', () => ({
  api: (...args: unknown[]) => state.request(...args),
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

describe('AdvancedPage', () => {
  const clipboard = Object.getOwnPropertyDescriptor(navigator, 'clipboard');
  const secureContext = Object.getOwnPropertyDescriptor(window, 'isSecureContext');

  beforeEach(() => {
    state.request.mockImplementation((path: string) => {
      if (path.includes('/sensitive-items') && !path.endsWith('/reveal')) return Promise.resolve({ items: state.items });
      if (path === '/api/controller/settings') return Promise.resolve({ timezone: 'UTC' });
      return Promise.resolve({});
    });
  });

  afterEach(() => {
    cleanup();
    state.request.mockReset();
    if (clipboard) Object.defineProperty(navigator, 'clipboard', clipboard);
    else Reflect.deleteProperty(navigator, 'clipboard');
    if (secureContext) Object.defineProperty(window, 'isSecureContext', secureContext);
    else Reflect.deleteProperty(window, 'isSecureContext');
  });

  it('names the cluster-scoped secret tab clearly', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <ConsoleContext.Provider value={{ clusters: [cluster], selectedCluster: cluster, selectedClusterId: 1, setSelectedClusterId: () => undefined, watchRun: () => undefined, refreshAll: async () => undefined }}>
          <AdvancedPage />
        </ConsoleContext.Provider>
      </QueryClientProvider>,
    );

    expect(await screen.findByRole('tab', { name: 'Elastic Stack Secret' })).toBeInTheDocument();
  });

  it('shows managed certificate and key paths below their host source', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <ConsoleContext.Provider value={{ clusters: [cluster], selectedCluster: cluster, selectedClusterId: 1, setSelectedClusterId: () => undefined, watchRun: () => undefined, refreshAll: async () => undefined }}>
          <AdvancedPage />
        </ConsoleContext.Provider>
      </QueryClientProvider>,
    );

    const certificatePath = await screen.findByText('/etc/elastic-control/clusters/lab-a/ca/ca.crt');
    expect(certificatePath.previousElementSibling).toHaveTextContent('node-a');
    expect(screen.getByText('/etc/elastic-control/clusters/lab-a/ca/ca.key')).toBeInTheDocument();
    const credential = screen.getByText('Elastic superuser password').closest('.sensitive-row');
    expect(credential).not.toBeNull();
    expect(within(credential as HTMLElement).queryByText('/etc/elastic-control/clusters/lab-a/ca/ca.crt')).not.toBeInTheDocument();
  });

  it('requires an explicit trusted copy click after re-authentication', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(window, 'isSecureContext', { configurable: true, value: true });
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
    state.request.mockImplementation((path: string) => {
      if (path.includes('/sensitive-items') && !path.endsWith('/reveal')) return Promise.resolve({ items: state.items });
      if (path === '/api/controller/settings') return Promise.resolve({ timezone: 'UTC' });
      if (path === '/api/auth/reveal-grants') return Promise.resolve({ grant_token: 'a'.repeat(32), expires_in: 60 });
      if (path.endsWith('/reveal')) return Promise.resolve({ value: 'super-secret', hide_after: 30 });
      return Promise.resolve({});
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <ConsoleContext.Provider value={{ clusters: [cluster], selectedCluster: cluster, selectedClusterId: 1, setSelectedClusterId: () => undefined, watchRun: () => undefined, refreshAll: async () => undefined }}>
          <AdvancedPage />
        </ConsoleContext.Provider>
      </QueryClientProvider>,
    );

    await screen.findByText('Elastic superuser password');
    const credential = screen.getByText('Elastic superuser password').closest('.sensitive-row');
    fireEvent.click(within(credential as HTMLElement).getByRole('button', { name: 'Copy' }));
    fireEvent.change(screen.getByLabelText('Administrator password'), { target: { value: 'current-password' } });
    fireEvent.click(screen.getByRole('button', { name: 'Authorize' }));

    expect(await screen.findByRole('button', { name: 'Copy to clipboard' })).toBeInTheDocument();
    expect(writeText).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Copy to clipboard' }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('super-secret'));
  });
});
