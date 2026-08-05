import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { describe, expect, it, vi } from 'vitest';
import { ConsoleContext } from '../../app-context';
import { MaintenanceWorkspace } from './MaintenanceWorkspace';

const state = vi.hoisted(() => ({
  enter: vi.fn(),
  exit: vi.fn(),
}));

vi.mock('../hosts', () => ({
  hostApi: { list: () => Promise.resolve([{ id: 1, name: 'node-a', address: '192.0.2.10', ssh_port: 22, ssh_user: 'root', enabled: true }]) },
}));

vi.mock('./api', () => ({
  maintenanceApi: {
    capabilities: () => Promise.resolve({ planning: true, operations: { host_reboot: false, rolling_restart: false, upgrade: false, evacuation: false }, backends: { documented_rolling: true, node_shutdown: false } }),
    manualMode: () => Promise.resolve({ node_id: 1, state: 'available', state_revision: 1, plan_id: null, run_id: null, expires_at: null, lifecycle_state: null }),
    listPlans: () => Promise.resolve({ count: 1, items: [{ plan_id: 'plan-1', lifecycle_state: 'ready', view: { header: { planId: 'plan-1', state: 'ready', target: { kind: 'host', name: 'node-a' }, operation: 'Manual maintenance', reason: 'Inspection', requester: 'operator', createdAt: '2026-08-04T00:00:00Z', freshness: { state: 'fresh' }, policy: { name: 'Effective maintenance policy', revision: 1, availabilityMode: 'zero-impact' } } } }] }),
    enterManualMode: state.enter,
    exitManualMode: state.exit,
  },
}));

const cluster = {
  id: 1, name: 'lab-a', slug: 'lab-a', theme_color: '#0077CC', desired_version: '8.20.0',
  ports: { elasticsearch_http: 9200, elasticsearch_transport: 9300, kibana: 5601, fleet: 8220, logstash_api: 9600 },
  network_defaults: { mode: 'shared' as const }, elasticsearch_settings: { allocation_enable: 'all' as const, rebalance_enable: 'all' as const, disk_watermark_low: '85%', disk_watermark_high: '90%', disk_watermark_flood_stage: '95%', recovery_max_bytes_per_sec: '40mb' },
  members: [{ cluster_id: 1, node_id: 1, name: 'node-a', address: '192.0.2.10', enabled: true, network_mode: 'shared' as const, data_interface: 'ens18', data_address: '192.0.2.10', user_interface: 'ens18', user_address: '192.0.2.10', network_ready: true }], assignments: [],
};

function renderWorkspace() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}><ConsoleContext.Provider value={{ clusters: [cluster], selectedCluster: cluster, selectedClusterId: 1, setSelectedClusterId: () => undefined, watchRun: () => undefined, refreshAll: async () => undefined }}><MaintenanceWorkspace /></ConsoleContext.Provider></QueryClientProvider>);
}

describe('MaintenanceWorkspace', () => {
  it('shows selected-cluster plan history and opens an in-page manual-maintenance form', async () => {
    state.enter.mockResolvedValue({ node_id: 1, state: 'maintenance', state_revision: 2, plan_id: 'plan-2', run_id: null, expires_at: null, lifecycle_state: 'ready' });
    renderWorkspace();
    expect(await screen.findByRole('heading', { name: 'Plan history' })).toBeInTheDocument();
    expect(screen.getByText('plan-1')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Enter maintenance mode' }));
    expect(screen.getByRole('heading', { name: 'Enter maintenance mode' })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Reason'), { target: { value: 'Kernel patching' } });
    fireEvent.click(screen.getAllByRole('button', { name: 'Enter maintenance mode' })[1]);
    await waitFor(() => expect(state.enter).toHaveBeenCalledWith(1, expect.objectContaining({ reason: 'Kernel patching', duration_seconds: 3600 })));
  });
});
