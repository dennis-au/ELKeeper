import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ConsoleContext } from '../app-context';
import type { Cluster } from '../features/clusters';
import type { SensitiveItem } from '../features/advanced';
import { AdvancedPage } from './AdvancedPage';

const state = vi.hoisted(() => ({
  items: [
    { id: 'cluster.elastic_password', label: 'Elastic superuser password', category: 'Credentials', source: 'controller', available: true, masked_value: '********' },
    { id: 'cluster.ca_certificate', label: 'Cluster CA certificate', category: 'Certificates', source: 'node-a', available: true, masked_value: '********', storage_path: '/etc/elastic-control/clusters/lab-a/ca/ca.crt' },
    { id: 'cluster.ca_private_key', label: 'Cluster CA private key', category: 'Private keys', source: 'node-a', available: true, masked_value: '********', storage_path: '/etc/elastic-control/clusters/lab-a/ca/ca.key' },
  ] as SensitiveItem[],
  request: vi.fn(),
}));

const certificateInventory = {
  items: [{
    id: 'asset-1', cluster_id: 1, trust_domain_id: 'domain-1', trust_domain: 'elasticsearch_transport',
    owner_type: 'assignment', owner_id: '1', purpose: 'elasticsearch_transport', provider_type: 'managed_legacy',
    management_state: 'observed', storage_locator: { node_name: 'node-a', path: '/etc/elastic-control/clusters/lab-a/workloads/ecp-lab-a-master-1/certs/node.crt' },
    desired_identity: {}, health: 'unobserved', last_observed_at: null, legacy_shared: true, split_migration_state: 'legacy_shared_detected',
  }],
  trust_domains: [{ id: 'domain-1', kind: 'elasticsearch_transport', legacy_shared: true, split_migration_state: 'legacy_shared_detected' }],
  compatibility: { version: '8.19.0', supported: true, format: 'PEM', reload_enabled: false, restart_required: true, profile: 'elastic-8-pem-rolling-restart-v1', mutation_enabled: false, mutation_blocker: 'rolling_restart_capability_disabled' },
};

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
      if (path === '/api/hosts/ssh-host-keys') return Promise.resolve({ items: [{ node_id: 1, name: 'node-a', address: '192.0.2.101', ssh_port: 22, fingerprint: 'SHA256:host-fingerprint' }] });
      if (path === '/api/clusters/1/certificates') return Promise.resolve(certificateInventory);
      if (path === '/api/clusters/1/certificate-policy') return Promise.resolve({ revision: 1, renew_before_days: 30, critical_before_days: 14, default_validity_days: 90, renewal_mode: 'approval_required' });
      if (path === '/api/clusters/1/certificate-operations') return Promise.resolve({ items: [] });
      if (path === '/api/clusters/1/certificate-trust-consumers') return Promise.resolve({ items: [], retirement_blocked: false, blockers: [] });
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

  it('keeps private keys metadata-only in the legacy secret inventory', async () => {
    state.items[2] = { ...state.items[2], reveal_deprecated: true, value_access: 'metadata_only' };
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <ConsoleContext.Provider value={{ clusters: [cluster], selectedCluster: cluster, selectedClusterId: 1, setSelectedClusterId: () => undefined, watchRun: () => undefined, refreshAll: async () => undefined }}>
          <AdvancedPage />
        </ConsoleContext.Provider>
      </QueryClientProvider>,
    );

    const privateKey = await screen.findByText('Cluster CA private key');
    const row = privateKey.closest('.sensitive-row');
    expect(within(row as HTMLElement).getByText('Metadata only')).toBeInTheDocument();
    expect(within(row as HTMLElement).queryByRole('button', { name: 'Reveal' })).not.toBeInTheDocument();
    expect(within(row as HTMLElement).queryByRole('button', { name: 'Copy' })).not.toBeInTheDocument();
    state.items[2] = { ...state.items[2], reveal_deprecated: undefined, value_access: undefined };
  });

  it('opens the Certificates workspace under Advanced', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <ConsoleContext.Provider value={{ clusters: [cluster], selectedCluster: cluster, selectedClusterId: 1, setSelectedClusterId: () => undefined, watchRun: () => undefined, refreshAll: async () => undefined }}>
          <AdvancedPage />
        </ConsoleContext.Provider>
      </QueryClientProvider>,
    );

    fireEvent.click(await screen.findByRole('tab', { name: 'Certificates' }));
    expect(await screen.findByRole('heading', { name: 'Certificate inventory' })).toBeInTheDocument();
    expect(screen.getByText('legacy shared')).toBeInTheDocument();
    expect(screen.getByText('PEM only')).toBeInTheDocument();
  });

  it('shows host-key fingerprints and confirms record deletion in-page', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <ConsoleContext.Provider value={{ clusters: [cluster], selectedCluster: cluster, selectedClusterId: 1, setSelectedClusterId: () => undefined, watchRun: () => undefined, refreshAll: async () => undefined }}>
          <AdvancedPage />
        </ConsoleContext.Provider>
      </QueryClientProvider>,
    );

    fireEvent.click(await screen.findByRole('tab', { name: 'SSH Host Keys' }));
    expect(await screen.findByText('SHA256:host-fingerprint')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Delete record' }));
    expect(screen.getByRole('heading', { name: 'Delete SSH host-key record for node-a' })).toBeInTheDocument();
    const confirmButton = document.querySelector('[data-test-subj="confirmModalConfirmButton"]');
    expect(confirmButton).not.toBeNull();
    fireEvent.click(confirmButton as HTMLButtonElement);
    await waitFor(() => expect(state.request).toHaveBeenCalledWith('/api/nodes/1/ssh-host-key', { method: 'DELETE' }));
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
