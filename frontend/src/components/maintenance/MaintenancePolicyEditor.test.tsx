import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { MaintenancePolicyResponse } from '../../types';
import { defaultMaintenancePolicy, MaintenancePolicyEditor } from './MaintenancePolicyEditor';

const state = vi.hoisted(() => ({
  planning: true,
  policyResponse: {
    policy: {
      max_unavailable: 1,
      max_surge: 0,
      minimum_master_eligible: 'quorum',
      minimum_data_per_tier: 1,
      minimum_kibana: 1,
      minimum_fleet_server: 1,
      minimum_logstash: 1,
      minimum_coordinating: 1,
      allow_agent_interruption: 'true-with-warning',
      required_cluster_health: 'green',
      allocation_guard: 'primaries-for-data',
      observation_max_age_seconds: 120,
      restart_allocation_delay_seconds: null,
      host_return_timeout_seconds: 900,
      workload_ready_timeout_seconds: 900,
      plan_validity_seconds: 300,
    },
    revision: 0,
    customized: false,
    updated_by: null,
    updated_at: null,
  } as MaintenancePolicyResponse,
  putResult: undefined as MaintenancePolicyResponse | undefined,
  putError: '' as string,
}));

const apiMock = vi.hoisted(() => vi.fn(async (path: string, options?: RequestInit) => {
  if (path !== '/api/clusters/7/maintenance-policy') throw new Error(`Unexpected API path: ${path}`);
  if (options?.method === 'PUT') {
    if (state.putError) throw new Error(state.putError);
    return state.putResult || state.policyResponse;
  }
  return state.policyResponse;
}));

vi.mock('../../api', () => ({
  api: apiMock,
  jsonBody: vi.fn((value) => ({ body: JSON.stringify(value), headers: { 'Content-Type': 'application/json' } })),
  queries: {
    maintenanceCapabilities: () => Promise.resolve({
      planning: state.planning,
      operations: { host_reboot: false, rolling_restart: false, upgrade: false, evacuation: false },
      backends: { documented_rolling: true, node_shutdown: false },
    }),
  },
}));

function renderEditor() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MaintenancePolicyEditor clusterId={7} />
    </QueryClientProvider>,
  );
}

describe('MaintenancePolicyEditor', () => {
  beforeEach(() => {
    state.planning = true;
    state.policyResponse = {
      policy: { ...defaultMaintenancePolicy },
      revision: 0,
      customized: false,
      updated_by: null,
      updated_at: null,
    };
    state.putResult = undefined;
    state.putError = '';
    apiMock.mockClear();
  });

  afterEach(cleanup);

  it('stays hidden and avoids the policy request when planning is disabled', async () => {
    state.planning = false;
    renderEditor();

    await waitFor(() => expect(screen.queryByRole('heading', { name: 'Maintenance policy' })).not.toBeInTheDocument());
    expect(apiMock).not.toHaveBeenCalled();
  });

  it('loads the default policy and revision metadata when planning is enabled', async () => {
    renderEditor();

    expect(await screen.findByRole('heading', { name: 'Maintenance policy' })).toBeInTheDocument();
    expect(screen.getByText('defaults')).toBeInTheDocument();
    expect(screen.getByText('revision 0')).toBeInTheDocument();
    expect(screen.getByLabelText('Maximum unavailable')).toHaveValue(1);
    expect(screen.getByLabelText('Minimum master eligible')).toHaveValue('quorum');
    expect(apiMock).toHaveBeenCalledWith('/api/clusters/7/maintenance-policy');
  });

  it('saves the complete policy with the current expected revision and adopts the response', async () => {
    state.policyResponse = {
      ...state.policyResponse,
      revision: 4,
      customized: true,
      updated_by: 'operator',
      updated_at: '2026-08-03T02:00:00Z',
    };
    state.putResult = {
      policy: { ...defaultMaintenancePolicy, max_unavailable: 2 },
      revision: 5,
      customized: true,
      updated_by: 'operator',
      updated_at: '2026-08-03T02:05:00Z',
    };
    renderEditor();

    fireEvent.change(await screen.findByLabelText('Maximum unavailable'), { target: { value: '2' } });
    fireEvent.change(screen.getByLabelText('Minimum master eligible'), { target: { value: '3' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save maintenance policy' }));

    await waitFor(() => expect(apiMock).toHaveBeenCalledWith(
      '/api/clusters/7/maintenance-policy',
      expect.objectContaining({ method: 'PUT' }),
    ));
    const request = apiMock.mock.calls.find(([, options]) => options?.method === 'PUT')?.[1];
    expect(JSON.parse(String(request?.body))).toEqual({
      expected_revision: 4,
      policy: { ...defaultMaintenancePolicy, max_unavailable: 2, minimum_master_eligible: 3 },
    });
    expect(await screen.findByText('revision 5')).toBeInTheDocument();
    expect(screen.getByText('customized')).toBeInTheDocument();
    expect(screen.getByText('Maintenance policy saved. New plans use this revision; existing plans remain immutable.')).toBeInTheDocument();
  });

  it('shows a revision conflict and reloads the latest policy into the form', async () => {
    state.policyResponse = { ...state.policyResponse, revision: 2, customized: true };
    state.putError = 'Maintenance policy revision changed';
    renderEditor();

    fireEvent.change(await screen.findByLabelText('Maximum unavailable'), { target: { value: '3' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save maintenance policy' }));
    expect(await screen.findByText('Policy update conflict')).toBeInTheDocument();

    state.policyResponse = {
      ...state.policyResponse,
      policy: { ...defaultMaintenancePolicy, max_unavailable: 4 },
      revision: 3,
    };
    fireEvent.click(screen.getByRole('button', { name: 'Reload latest policy' }));

    await waitFor(() => expect(screen.getByLabelText('Maximum unavailable')).toHaveValue(4));
    expect(screen.getByText('revision 3')).toBeInTheDocument();
    expect(screen.queryByText('Policy update conflict')).not.toBeInTheDocument();
  });

  it('disables save for invalid bounded values and minimum-master input', async () => {
    renderEditor();

    const maximumUnavailable = await screen.findByLabelText('Maximum unavailable');
    fireEvent.change(maximumUnavailable, { target: { value: '0' } });
    expect(screen.getByRole('button', { name: 'Save maintenance policy' })).toBeDisabled();
    expect(screen.getByText('Policy values are invalid')).toBeInTheDocument();

    fireEvent.change(maximumUnavailable, { target: { value: '2' } });
    fireEvent.change(screen.getByLabelText('Minimum master eligible'), { target: { value: 'invalid' } });
    expect(screen.getByRole('button', { name: 'Save maintenance policy' })).toBeDisabled();
  });
});
