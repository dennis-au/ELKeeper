import { useEffect, useMemo, useState } from 'react';
import {
  EuiBadge, EuiBasicTable, EuiButton, EuiButtonEmpty, EuiCallOut, EuiFieldNumber,
  EuiFormRow, EuiModal, EuiModalBody, EuiModalFooter, EuiModalHeader, EuiModalHeaderTitle,
  EuiOverlayMask, EuiSelect, EuiSpacer, EuiTab, EuiTabs, EuiText, EuiTitle,
} from '@elastic/eui';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useConsole } from '../../app-context';
import { formatDateTime } from '../../shared/format';
import { certificatesApi } from './api';
import type { CertificateAsset, CertificateOperation, CertificatePolicy, CertificatePreview } from './types';

type View = 'inventory' | 'policy' | 'operations' | 'consumers';

const healthColor: Record<string, 'success' | 'warning' | 'danger' | 'hollow'> = {
  healthy: 'success', renewal_due: 'warning', renewal_overdue: 'danger', expired: 'danger',
  unobserved: 'hollow', observation_failed: 'warning', mismatched: 'danger', invalid_chain: 'danger',
};

function readable(value: string) {
  return value.replaceAll('_', ' ');
}

function previewTitle(preview: CertificatePreview) {
  return preview.operation_type === 'ca_rotation' ? 'CA rotation preview' : 'Certificate renewal preview';
}

export function CertificatesPanel() {
  const { selectedCluster, watchRun } = useConsole();
  const client = useQueryClient();
  const [view, setView] = useState<View>('inventory');
  const [draft, setDraft] = useState<CertificatePolicy>();
  const [error, setError] = useState('');
  const [preview, setPreview] = useState<CertificatePreview>();
  const clusterId = selectedCluster?.id;
  const inventory = useQuery({ queryKey: ['certificates', clusterId], enabled: Boolean(clusterId), queryFn: () => certificatesApi.inventory(clusterId!) });
  const policy = useQuery({ queryKey: ['certificate-policy', clusterId], enabled: Boolean(clusterId), queryFn: () => certificatesApi.policy(clusterId!) });
  const operations = useQuery({ queryKey: ['certificate-operations', clusterId], enabled: Boolean(clusterId), queryFn: () => certificatesApi.operations(clusterId!) });
  const consumers = useQuery({ queryKey: ['certificate-trust-consumers', clusterId], enabled: Boolean(clusterId), queryFn: () => certificatesApi.consumers(clusterId!) });
  useEffect(() => { setDraft(policy.data); }, [policy.data]);
  useEffect(() => { setError(''); setPreview(undefined); setView('inventory'); }, [clusterId]);
  const timezone = 'UTC';
  const compatibility = inventory.data?.compatibility;
  const refresh = async () => {
    if (!clusterId) return;
    setError('');
    try {
      const result = await certificatesApi.refresh(clusterId);
      if (result.run_id) watchRun(result.run_id);
      await Promise.all([
        client.invalidateQueries({ queryKey: ['certificates', clusterId] }),
        client.invalidateQueries({ queryKey: ['certificate-operations', clusterId] }),
      ]);
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'Unable to refresh certificate inventory.'); }
  };
  const createRenewalPreview = async (asset: CertificateAsset) => {
    setError('');
    try {
      setPreview(await certificatesApi.renewalPreview(asset.id));
      await client.invalidateQueries({ queryKey: ['certificate-operations', clusterId] });
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'Unable to create a renewal preview.'); }
  };
  const createCaPreview = async () => {
    if (!clusterId) return;
    setError('');
    try {
      setPreview(await certificatesApi.caRotationPreview(clusterId));
      await client.invalidateQueries({ queryKey: ['certificate-operations', clusterId] });
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'Unable to create a CA rotation preview.'); }
  };
  const savePolicy = async () => {
    if (!clusterId || !draft) return;
    setError('');
    try {
      await certificatesApi.updatePolicy(clusterId, { ...draft, expected_revision: draft.revision });
      await client.invalidateQueries({ queryKey: ['certificate-policy', clusterId] });
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'Unable to save the certificate policy.'); }
  };
  const inventoryColumns = useMemo(() => [
    { field: 'purpose', name: 'Purpose', render: (value: string, item: CertificateAsset) => <div><strong>{readable(value)}</strong><small>{item.storage_locator.node_name || item.owner_type}</small></div> },
    { field: 'trust_domain', name: 'Trust domain', render: (value: string, item: CertificateAsset) => <div><span>{readable(value)}</span>{item.legacy_shared && <small>legacy shared</small>}</div> },
    { field: 'health', name: 'Health', render: (value: string) => <EuiBadge color={healthColor[value] || 'hollow'}>{readable(value)}</EuiBadge> },
    { field: 'management_state', name: 'Management', render: (value: string) => <EuiBadge color={value === 'managed' ? 'success' : 'warning'}>{readable(value)}</EuiBadge> },
    { field: 'last_observed_at', name: 'Observed', render: (value: string | null) => value ? formatDateTime(value, timezone) : 'Not collected' },
    { field: 'id', name: 'Actions', render: (_value: string, item: CertificateAsset) => <EuiButtonEmpty size="s" iconType="inspect" onClick={() => void createRenewalPreview(item)}>Preview renewal</EuiButtonEmpty> },
  ], [clusterId]);
  const operationColumns = useMemo(() => [
    { field: 'operation_type', name: 'Operation', render: (value: string) => readable(value) },
    { field: 'state', name: 'State', render: (value: string) => <EuiBadge color={value === 'ready' ? 'success' : value === 'blocked' ? 'warning' : 'hollow'}>{readable(value)}</EuiBadge> },
    { field: 'blockers', name: 'Blockers', render: (value: string[]) => value.length ? value.map(readable).join(', ') : 'None' },
    { field: 'created_at', name: 'Created', render: (value: string) => formatDateTime(value, timezone) },
  ], []);
  const consumerColumns = useMemo(() => [
    { field: 'description', name: 'Consumer', render: (value: string, item: { consumer_kind: string }) => <div><strong>{value}</strong><small>{readable(item.consumer_kind)}</small></div> },
    { field: 'trust_domain', name: 'Trust domain', render: (value: string) => readable(value) },
    { field: 'consumer_type', name: 'Type', render: (value: string) => <EuiBadge color={value === 'managed' ? 'success' : 'warning'}>{value}</EuiBadge> },
    { field: 'trust_state', name: 'Trust', render: (value: string) => readable(value) },
    { field: 'last_verified_at', name: 'Verified', render: (value: string | null) => value ? formatDateTime(value, timezone) : 'Not verified' },
  ], []);

  if (!selectedCluster) return <EuiCallOut title="Select or create a cluster" iconType="cluster">Certificate inventory and policy are scoped to one cluster.</EuiCallOut>;
  return <div className="certificates-tab">
    <div className="section-heading"><div><EuiTitle size="s"><h2>Certificates</h2></EuiTitle><EuiText color="subdued">Lifecycle inventory, policy, trust consumers, and approval-required previews.</EuiText></div><EuiButtonEmpty iconType="refresh" onClick={() => void refresh()} isLoading={inventory.isFetching}>Refresh inventory</EuiButtonEmpty></div>
    {error && <EuiCallOut title="Certificate action failed" color="danger" iconType="warning">{error}</EuiCallOut>}
    {compatibility && <EuiCallOut title={compatibility.supported ? 'Mutation gate is active' : 'Unsupported Elastic TLS profile'} color={compatibility.supported ? 'warning' : 'danger'} iconType="lock">{compatibility.supported ? `${compatibility.format} only. Certificate renewal and CA rotation require the shared rolling-restart executor before execution can be enabled.` : `${compatibility.version || 'Unknown version'} has no approved certificate lifecycle profile.`}</EuiCallOut>}
    <EuiTabs>
      <EuiTab isSelected={view === 'inventory'} onClick={() => setView('inventory')}>Inventory</EuiTab>
      <EuiTab isSelected={view === 'policy'} onClick={() => setView('policy')}>Policy</EuiTab>
      <EuiTab isSelected={view === 'operations'} onClick={() => setView('operations')}>Operations</EuiTab>
      <EuiTab isSelected={view === 'consumers'} onClick={() => setView('consumers')}>Trust Consumers</EuiTab>
    </EuiTabs>
    {view === 'inventory' && <section className="section-band"><div className="section-heading"><EuiTitle size="s"><h2>Certificate inventory</h2></EuiTitle><div className="form-actions"><EuiBadge>{inventory.data?.items.length || 0}</EuiBadge><EuiBadge color="hollow">PEM only</EuiBadge><EuiButtonEmpty iconType="refresh" onClick={() => void createCaPreview()}>Preview CA rotation</EuiButtonEmpty></div></div>
      {inventory.isLoading ? <EuiText color="subdued">Loading certificate inventory...</EuiText> : <EuiBasicTable items={inventory.data?.items || []} columns={inventoryColumns} noItemsMessage="No certificate assets are configured." />}
      {inventory.data?.trust_domains.some((item) => item.legacy_shared) && <EuiCallOut title="Legacy shared certificate layout" color="warning" iconType="alert">Transport and HTTP are currently projected from shared legacy material. ELKeeper will require a split migration preview before any renewal or CA rotation.</EuiCallOut>}
    </section>}
    {view === 'policy' && <section className="section-band"><div className="section-heading"><div><EuiTitle size="s"><h2>Renewal policy</h2></EuiTitle><EuiText color="subdued">Scheduler proposals require approval and never start certificate mutation automatically.</EuiText></div><EuiBadge color="warning">approval required</EuiBadge></div>
      {draft && <div className="form-grid"><EuiFormRow label="Renew before (days)"><EuiFieldNumber min={1} value={draft.renew_before_days} onChange={(event) => setDraft({ ...draft, renew_before_days: Number(event.target.value) })} /></EuiFormRow><EuiFormRow label="Critical before (days)"><EuiFieldNumber min={1} value={draft.critical_before_days} onChange={(event) => setDraft({ ...draft, critical_before_days: Number(event.target.value) })} /></EuiFormRow><EuiFormRow label="Leaf validity (days)"><EuiFieldNumber min={1} value={draft.default_validity_days} onChange={(event) => setDraft({ ...draft, default_validity_days: Number(event.target.value) })} /></EuiFormRow><EuiFormRow label="Renewal mode"><EuiSelect value={draft.renewal_mode} options={[{ value: 'manual', text: 'Manual' }, { value: 'approval_required', text: 'Approval required' }, { value: 'scheduled', text: 'Scheduled preview' }]} onChange={(event) => setDraft({ ...draft, renewal_mode: event.target.value as CertificatePolicy['renewal_mode'] })} /></EuiFormRow></div>}
      <EuiSpacer size="s" /><EuiButton fill onClick={() => void savePolicy()} disabled={!draft}>Save policy</EuiButton>
    </section>}
    {view === 'operations' && <section className="section-band"><div className="section-heading"><EuiTitle size="s"><h2>Lifecycle operations</h2></EuiTitle><EuiBadge>{operations.data?.items.length || 0}</EuiBadge></div><EuiBasicTable items={operations.data?.items || []} columns={operationColumns} loading={operations.isLoading} noItemsMessage="No renewal or CA rotation previews exist." /></section>}
    {view === 'consumers' && <section className="section-band"><div className="section-heading"><div><EuiTitle size="s"><h2>Trust consumers</h2></EuiTitle><EuiText color="subdued">Unverified external consumers block old CA retirement.</EuiText></div>{consumers.data?.retirement_blocked && <EuiBadge color="danger">retirement blocked</EuiBadge>}</div><EuiBasicTable items={consumers.data?.items || []} columns={consumerColumns} loading={consumers.isLoading} noItemsMessage="No trust consumers are declared." /></section>}
    {preview && <EuiOverlayMask><EuiModal onClose={() => setPreview(undefined)}><EuiModalHeader><EuiModalHeaderTitle>{previewTitle(preview)}</EuiModalHeaderTitle></EuiModalHeader><EuiModalBody><EuiCallOut title={preview.execution_enabled ? 'Ready for approval' : 'Execution remains blocked'} color={preview.execution_enabled ? 'success' : 'warning'} iconType={preview.execution_enabled ? 'check' : 'lock'}>{preview.blockers.length ? preview.blockers.map(readable).join(', ') : 'Review the affected workloads before approval.'}</EuiCallOut><EuiSpacer /><EuiText size="s"><p>This preview does not generate, stage, activate, restart, or retire certificate material.</p></EuiText></EuiModalBody><EuiModalFooter><EuiButtonEmpty onClick={() => setPreview(undefined)}>Close</EuiButtonEmpty></EuiModalFooter></EuiModal></EuiOverlayMask>}
  </div>;
}
