import { useEffect, useMemo, useState } from 'react';
import {
  EuiBadge,
  EuiButton,
  EuiButtonEmpty,
  EuiCallOut,
  EuiFieldNumber,
  EuiFieldText,
  EuiFlexGroup,
  EuiFlexItem,
  EuiFormRow,
  EuiProgress,
  EuiSelect,
  EuiSpacer,
  EuiSwitch,
  EuiText,
  EuiTitle,
} from '@elastic/eui';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api, jsonBody, queries } from '../../api';
import { formatDateTime } from '../../format';
import type { MaintenancePolicy, MaintenancePolicyResponse } from '../../types';

type MaintenancePolicyForm = Omit<MaintenancePolicy, 'minimum_master_eligible'> & {
  minimum_master_eligible: MaintenancePolicy['minimum_master_eligible'] | string;
};

export const defaultMaintenancePolicy: MaintenancePolicy = {
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
};

const integerBounds: Array<[keyof MaintenancePolicy, number, number]> = [
  ['max_unavailable', 1, 100],
  ['minimum_data_per_tier', 1, 100],
  ['minimum_kibana', 1, 100],
  ['minimum_fleet_server', 1, 100],
  ['minimum_logstash', 1, 100],
  ['minimum_coordinating', 1, 100],
  ['observation_max_age_seconds', 1, 3600],
  ['host_return_timeout_seconds', 30, 86400],
  ['workload_ready_timeout_seconds', 30, 86400],
  ['plan_validity_seconds', 30, 3600],
];

function isBoundedInteger(value: unknown, minimum: number, maximum: number) {
  return typeof value === 'number' && Number.isInteger(value) && value >= minimum && value <= maximum;
}

function validMinimumMaster(value: MaintenancePolicyForm['minimum_master_eligible']) {
  if (value === 'quorum') return true;
  const normalized = typeof value === 'string' ? Number(value) : value;
  return String(value).trim() !== '' && isBoundedInteger(normalized, 1, 100);
}

function validPolicy(policy: MaintenancePolicyForm) {
  if (!integerBounds.every(([key, minimum, maximum]) => isBoundedInteger(policy[key], minimum, maximum))) return false;
  if (!validMinimumMaster(policy.minimum_master_eligible)) return false;
  return policy.restart_allocation_delay_seconds === null
    || isBoundedInteger(policy.restart_allocation_delay_seconds, 0, 86400);
}

function normalizedPolicy(policy: MaintenancePolicyForm): MaintenancePolicy {
  return {
    ...policy,
    minimum_master_eligible: policy.minimum_master_eligible === 'quorum'
      ? 'quorum'
      : Number(policy.minimum_master_eligible),
  };
}

function isRevisionConflict(error: string) {
  return /revision|conflict|changed|stale/i.test(error);
}

export function MaintenancePolicyEditor({ clusterId }: { clusterId: number }) {
  const queryClient = useQueryClient();
  const [policy, setPolicy] = useState<MaintenancePolicyForm>(defaultMaintenancePolicy);
  const [message, setMessage] = useState('');
  const [conflict, setConflict] = useState(false);
  const { data: capabilities } = useQuery({
    queryKey: ['maintenance-capabilities'],
    queryFn: queries.maintenanceCapabilities,
    retry: false,
  });
  const enabled = capabilities?.planning === true;
  const policyQuery = useQuery({
    queryKey: ['maintenance-policy', clusterId],
    queryFn: () => api<MaintenancePolicyResponse>(`/api/clusters/${clusterId}/maintenance-policy`),
    enabled,
    retry: false,
  });
  const response = policyQuery.data;
  useEffect(() => {
    if (response?.policy) setPolicy({ ...defaultMaintenancePolicy, ...response.policy });
  }, [response]);
  useEffect(() => {
    setMessage('');
    setConflict(false);
  }, [clusterId]);

  const dirty = useMemo(() => Boolean(response) && JSON.stringify(policy) !== JSON.stringify(response?.policy), [policy, response]);
  const mutation = useMutation({
    mutationFn: () => api<MaintenancePolicyResponse>(`/api/clusters/${clusterId}/maintenance-policy`, {
      method: 'PUT',
      ...jsonBody({ expected_revision: response?.revision ?? 0, policy: normalizedPolicy(policy) }),
    }),
    onSuccess: (updated) => {
      queryClient.setQueryData(['maintenance-policy', clusterId], updated);
      setPolicy(updated.policy);
      setConflict(false);
      setMessage('Maintenance policy saved. New plans use this revision; existing plans remain immutable.');
    },
    onError: (error) => {
      const detail = error instanceof Error ? error.message : 'Unable to save maintenance policy';
      setConflict(isRevisionConflict(detail));
      setMessage(detail);
    },
  });
  const setNumber = (key: keyof MaintenancePolicy, value: number) => setPolicy((current) => ({ ...current, [key]: value }));
  const reload = async () => {
    setMessage(''); setConflict(false);
    await policyQuery.refetch();
  };

  if (!enabled) return null;
  if (policyQuery.isLoading) return <section className="section-band"><EuiProgress size="xs" color="accent" /><EuiText size="s" color="subdued">Loading maintenance policy...</EuiText></section>;
  if (policyQuery.error || !response) return <section className="section-band"><EuiCallOut title="Maintenance policy is unavailable" color="danger" iconType="warning">{policyQuery.error instanceof Error ? policyQuery.error.message : 'The controller did not return a policy.'}<EuiSpacer size="s" /><EuiButtonEmpty size="s" iconType="refresh" onClick={() => policyQuery.refetch()}>Retry</EuiButtonEmpty></EuiCallOut></section>;

  return (
    <section className="section-band" aria-labelledby="maintenance-policy-heading">
      <div className="section-heading">
        <div>
          <EuiTitle size="s"><h2 id="maintenance-policy-heading">Maintenance policy</h2></EuiTitle>
          <EuiText color="subdued">Safety budgets and freshness limits used when compiling new maintenance plans.</EuiText>
        </div>
        <EuiFlexGroup gutterSize="s" responsive={false} alignItems="center" wrap>
          <EuiFlexItem grow={false}><EuiBadge color={response.customized ? 'primary' : 'hollow'}>{response.customized ? 'customized' : 'defaults'}</EuiBadge></EuiFlexItem>
          <EuiFlexItem grow={false}><EuiBadge color="hollow">revision {response.revision}</EuiBadge></EuiFlexItem>
        </EuiFlexGroup>
      </div>
      {message && <><EuiCallOut
        size="s"
        title={conflict ? 'Policy update conflict' : mutation.isError ? 'Maintenance policy update failed' : 'Policy saved'}
        color={conflict || mutation.isError ? 'danger' : 'success'}
        iconType={conflict || mutation.isError ? 'warning' : 'check'}
      >
        {message}
        {conflict && <><EuiSpacer size="s" /><EuiButtonEmpty size="s" iconType="refresh" onClick={reload}>Reload latest policy</EuiButtonEmpty></>}
      </EuiCallOut><EuiSpacer size="m" /></>}

      <EuiTitle size="xxs"><h3>Availability budgets</h3></EuiTitle>
      <EuiSpacer size="s" />
      <div className="form-grid">
        <EuiFormRow label="Maximum unavailable"><EuiFieldNumber min={1} max={100} value={policy.max_unavailable} onChange={(event) => setNumber('max_unavailable', Number(event.target.value))} /></EuiFormRow>
        <EuiFormRow label="Maximum surge" helpText="Reserved for replacement workflows and fixed at zero."><EuiFieldNumber value={0} disabled /></EuiFormRow>
        <EuiFormRow label="Minimum master eligible" helpText="Use quorum or an integer from 1 through 100."><EuiFieldText value={policy.minimum_master_eligible} onChange={(event) => setPolicy((current) => ({ ...current, minimum_master_eligible: event.target.value.trim().toLowerCase() === 'quorum' ? 'quorum' : event.target.value }))} /></EuiFormRow>
        <EuiFormRow label="Minimum data nodes per tier"><EuiFieldNumber min={1} max={100} value={policy.minimum_data_per_tier} onChange={(event) => setNumber('minimum_data_per_tier', Number(event.target.value))} /></EuiFormRow>
        <EuiFormRow label="Minimum Kibana"><EuiFieldNumber min={1} max={100} value={policy.minimum_kibana} onChange={(event) => setNumber('minimum_kibana', Number(event.target.value))} /></EuiFormRow>
        <EuiFormRow label="Minimum Fleet Server"><EuiFieldNumber min={1} max={100} value={policy.minimum_fleet_server} onChange={(event) => setNumber('minimum_fleet_server', Number(event.target.value))} /></EuiFormRow>
        <EuiFormRow label="Minimum Logstash"><EuiFieldNumber min={1} max={100} value={policy.minimum_logstash} onChange={(event) => setNumber('minimum_logstash', Number(event.target.value))} /></EuiFormRow>
        <EuiFormRow label="Minimum coordinating"><EuiFieldNumber min={1} max={100} value={policy.minimum_coordinating} onChange={(event) => setNumber('minimum_coordinating', Number(event.target.value))} /></EuiFormRow>
      </div>
      <EuiSpacer size="m" />
      <EuiFlexGroup gutterSize="l" wrap>
        <EuiFlexItem grow={false}><EuiSwitch label="Allow agent interruption with warning" checked={policy.allow_agent_interruption === 'true-with-warning'} onChange={(event) => setPolicy((current) => ({ ...current, allow_agent_interruption: event.target.checked ? 'true-with-warning' : 'block' }))} /></EuiFlexItem>
        <EuiFlexItem grow={false}><EuiSwitch label="Guard data allocation during restart" checked={policy.allocation_guard === 'primaries-for-data'} onChange={(event) => setPolicy((current) => ({ ...current, allocation_guard: event.target.checked ? 'primaries-for-data' : 'none' }))} /></EuiFlexItem>
      </EuiFlexGroup>

      <EuiSpacer size="l" />
      <EuiTitle size="xxs"><h3>Safety and timeouts</h3></EuiTitle>
      <EuiSpacer size="s" />
      <div className="form-grid">
        <EuiFormRow label="Required cluster health"><EuiSelect value={policy.required_cluster_health} onChange={(event) => setPolicy((current) => ({ ...current, required_cluster_health: event.target.value as MaintenancePolicy['required_cluster_health'] }))} options={[{ value: 'green', text: 'Green' }, { value: 'yellow', text: 'Yellow or better' }]} /></EuiFormRow>
        <EuiFormRow label="Observation maximum age (seconds)"><EuiFieldNumber min={1} max={3600} value={policy.observation_max_age_seconds} onChange={(event) => setNumber('observation_max_age_seconds', Number(event.target.value))} /></EuiFormRow>
        <EuiFormRow label="Restart allocation delay (seconds)" helpText="Blank uses the Elasticsearch default."><EuiFieldNumber min={0} max={86400} value={policy.restart_allocation_delay_seconds ?? ''} onChange={(event) => setPolicy((current) => ({ ...current, restart_allocation_delay_seconds: event.target.value === '' ? null : Number(event.target.value) }))} /></EuiFormRow>
        <EuiFormRow label="Host return timeout (seconds)"><EuiFieldNumber min={30} max={86400} value={policy.host_return_timeout_seconds} onChange={(event) => setNumber('host_return_timeout_seconds', Number(event.target.value))} /></EuiFormRow>
        <EuiFormRow label="Workload readiness timeout (seconds)"><EuiFieldNumber min={30} max={86400} value={policy.workload_ready_timeout_seconds} onChange={(event) => setNumber('workload_ready_timeout_seconds', Number(event.target.value))} /></EuiFormRow>
        <EuiFormRow label="Plan validity (seconds)"><EuiFieldNumber min={30} max={3600} value={policy.plan_validity_seconds} onChange={(event) => setNumber('plan_validity_seconds', Number(event.target.value))} /></EuiFormRow>
      </div>
      {!validPolicy(policy) && <><EuiSpacer size="s" /><EuiCallOut size="s" title="Policy values are invalid" color="danger" iconType="warning">Use the limits shown beside each field before saving.</EuiCallOut></>}
      <EuiSpacer size="m" />
      <EuiFlexGroup gutterSize="s" alignItems="center" wrap>
        <EuiFlexItem grow={false}><EuiButton fill onClick={() => mutation.mutate()} isLoading={mutation.isPending} disabled={!dirty || !validPolicy(policy)}>Save maintenance policy</EuiButton></EuiFlexItem>
        <EuiFlexItem grow={false}><EuiButtonEmpty iconType="refresh" onClick={reload} disabled={mutation.isPending}>Reload</EuiButtonEmpty></EuiFlexItem>
        <EuiFlexItem grow={false}><EuiText size="xs" color="subdued">Updated {response.updated_at ? formatDateTime(response.updated_at) : 'never'}{response.updated_by ? ` by ${response.updated_by}` : ''}</EuiText></EuiFlexItem>
      </EuiFlexGroup>
    </section>
  );
}
