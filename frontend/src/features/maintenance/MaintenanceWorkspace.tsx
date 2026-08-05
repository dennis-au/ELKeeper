import { useMemo, useState } from 'react';
import {
  EuiBadge, EuiBasicTable, EuiButton, EuiButtonEmpty, EuiCallOut, EuiConfirmModal,
  EuiFieldNumber, EuiFieldText, EuiFormRow, EuiHealth, EuiModal, EuiModalBody,
  EuiModalFooter, EuiModalHeader, EuiModalHeaderTitle, EuiOverlayMask, EuiPanel,
  EuiSelect, EuiSpacer, EuiText, EuiTitle,
} from '@elastic/eui';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useConsole } from '../../app-context';
import { hostApi } from '../hosts';
import { formatDateTime } from '../../shared/format';
import { maintenanceApi } from './api';
import { MaintenancePlanPreview } from './components';
import type { MaintenancePlanViewModel } from './components';
import type { MaintenancePlanHistoryItem, MaintenancePlanHistoryResponse } from './types';

const stateColor: Record<string, 'success' | 'warning' | 'danger' | 'primary' | 'hollow'> = {
  available: 'success', planning: 'warning', maintenance: 'primary', recovery_required: 'danger',
  ready: 'success', succeeded: 'success', blocked: 'danger', failed: 'danger', cancelled: 'warning', paused: 'warning', executing: 'primary',
};

function idempotencyKey(nodeId: number) {
  const value = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`;
  return `manual-maintenance-${nodeId}-${value}`.slice(0, 128);
}

export function MaintenanceWorkspace() {
  const queryClient = useQueryClient();
  const { selectedCluster, selectedClusterId, watchRun } = useConsole();
  const [nodeId, setNodeId] = useState<number>();
  const [enterOpen, setEnterOpen] = useState(false);
  const [exitOpen, setExitOpen] = useState(false);
  const [detail, setDetail] = useState<MaintenancePlanHistoryItem>();
  const [reason, setReason] = useState('Planned operator maintenance');
  const [duration, setDuration] = useState(3600);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const { data: capabilities } = useQuery({ queryKey: ['maintenance-capabilities'], queryFn: maintenanceApi.capabilities, retry: false });
  const { data: nodes = [] } = useQuery({ queryKey: ['nodes'], queryFn: hostApi.list });
  const clusterNodes = useMemo(() => selectedCluster
    ? nodes.filter((node) => selectedCluster.members.some((member) => member.node_id === node.id))
    : nodes, [nodes, selectedCluster]);
  const selectedNodeId = nodeId || clusterNodes[0]?.id;
  const { data: mode, refetch: refetchMode } = useQuery({
    queryKey: ['manual-maintenance-mode', selectedNodeId], enabled: Boolean(selectedNodeId),
    queryFn: () => maintenanceApi.manualMode(selectedNodeId!),
  });
  const { data: history, isLoading: historyLoading, refetch: refetchHistory } = useQuery<MaintenancePlanHistoryResponse>({
    queryKey: ['maintenance-plans', selectedClusterId],
    queryFn: () => maintenanceApi.listPlans<MaintenancePlanHistoryResponse>({ cluster_id: selectedClusterId, limit: 20 }),
  });
  const refresh = async () => { await Promise.all([refetchMode(), refetchHistory()]); };
  const enter = async () => {
    if (!selectedNodeId || !reason.trim()) return;
    setBusy(true); setError('');
    try {
      const result = await maintenanceApi.enterManualMode(selectedNodeId, { reason: reason.trim(), duration_seconds: duration || undefined, idempotency_key: idempotencyKey(selectedNodeId) });
      if (result.run_id) watchRun(result.run_id);
      setEnterOpen(false); await refresh();
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'Unable to enter maintenance mode.'); }
    finally { setBusy(false); }
  };
  const exit = async () => {
    if (!selectedNodeId) return;
    setBusy(true); setError('');
    try {
      const result = await maintenanceApi.exitManualMode(selectedNodeId, { reason: 'Operator returned host to service' });
      if (result.run_id) watchRun(result.run_id);
      setExitOpen(false); await refresh();
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'Unable to exit maintenance mode.'); }
    finally { setBusy(false); }
  };
  const columns = [
    { field: 'plan_id', name: 'Plan', render: (value: string) => <EuiButtonEmpty size="s" onClick={() => setDetail(history?.items.find((item) => item.plan_id === value))}>{value}</EuiButtonEmpty> },
    { field: 'view.header.operation', name: 'Operation', render: (_: unknown, item: MaintenancePlanHistoryItem) => item.view.header.operation },
    { field: 'view.header.target.name', name: 'Target', render: (_: unknown, item: MaintenancePlanHistoryItem) => item.view.header.target.name },
    { field: 'lifecycle_state', name: 'State', render: (value: string) => <EuiBadge color={stateColor[value] || 'hollow'}>{value.replaceAll('_', ' ')}</EuiBadge> },
    { field: 'view.header.createdAt', name: 'Created', render: (_: unknown, item: MaintenancePlanHistoryItem) => formatDateTime(item.view.header.createdAt) },
  ];
  const modeState = mode?.state || 'available';
  return <div className="page-stack">
    <div className="page-heading"><div><EuiTitle><h1>Maintenance</h1></EuiTitle><EuiText color="subdued">Review persisted maintenance plans and place a selected host into an operator-controlled maintenance window.</EuiText></div><EuiButtonEmpty iconType="refresh" onClick={() => void refresh()} isLoading={historyLoading}>Refresh</EuiButtonEmpty></div>
    {!selectedCluster && <EuiCallOut title="Select a cluster" iconType="cluster">Plan history is scoped to the selected cluster. Host maintenance controls remain available for enrolled hosts.</EuiCallOut>}
    {capabilities?.planning === false && <EuiCallOut title="Maintenance planning is disabled" color="warning" iconType="lock">Plan history remains available, but manual maintenance cannot be entered until its safety capability is enabled.</EuiCallOut>}
    {error && <EuiCallOut title="Maintenance action failed" color="danger" iconType="warning">{error}</EuiCallOut>}
    <section className="section-band"><div className="section-heading"><EuiTitle size="s"><h2>Manual maintenance mode</h2></EuiTitle><EuiBadge color={stateColor[modeState] || 'hollow'}>{modeState.replaceAll('_', ' ')}</EuiBadge></div>
      <EuiPanel hasBorder paddingSize="m"><div className="form-grid"><EuiFormRow label="Host"><EuiSelect aria-label="Maintenance host" value={selectedNodeId || ''} onChange={(event) => setNodeId(Number(event.target.value))} options={clusterNodes.length ? clusterNodes.map((node) => ({ value: node.id, text: `${node.name} (${node.address})` })) : [{ value: '', text: 'No eligible hosts' }]} /></EuiFormRow><EuiFormRow label="Active plan"><EuiFieldText readOnly value={mode?.plan_id || 'None'} /></EuiFormRow></div><EuiSpacer size="m" />
      {mode?.expires_at && <EuiText size="s" color="subdued">Window expires {formatDateTime(mode.expires_at)}.</EuiText>}
      <EuiSpacer size="s" /><EuiButton fill iconType="wrench" onClick={() => { setError(''); setEnterOpen(true); }} disabled={!selectedNodeId || capabilities?.planning !== true || modeState !== 'available'}>Enter maintenance mode</EuiButton>{' '}<EuiButton color="danger" iconType="play" onClick={() => { setError(''); setExitOpen(true); }} disabled={!selectedNodeId || modeState !== 'maintenance'}>Exit maintenance mode</EuiButton></EuiPanel>
    </section>
    <section className="section-band"><div className="section-heading"><EuiTitle size="s"><h2>Plan history</h2></EuiTitle><EuiBadge>{history?.count || 0}</EuiBadge></div><EuiBasicTable items={history?.items || []} columns={columns} loading={historyLoading} noItemsMessage="No maintenance plans match the selected cluster." /></section>
    {enterOpen && <EuiOverlayMask><EuiModal onClose={() => setEnterOpen(false)} initialFocus="[data-autofocus]"><EuiModalHeader><EuiModalHeaderTitle>Enter maintenance mode</EuiModalHeaderTitle></EuiModalHeader><EuiModalBody><EuiText><p>The host remains operator-controlled until you explicitly exit the window or it expires.</p></EuiText><EuiSpacer /><EuiFormRow label="Reason"><EuiFieldText data-autofocus value={reason} onChange={(event) => setReason(event.target.value)} /></EuiFormRow><EuiFormRow label="Duration (seconds)"><EuiFieldNumber min={60} value={duration} onChange={(event) => setDuration(Number(event.target.value))} /></EuiFormRow></EuiModalBody><EuiModalFooter><EuiButtonEmpty onClick={() => setEnterOpen(false)}>Cancel</EuiButtonEmpty><EuiButton fill onClick={() => void enter()} isLoading={busy} disabled={!reason.trim()}>Enter maintenance mode</EuiButton></EuiModalFooter></EuiModal></EuiOverlayMask>}
    {exitOpen && <EuiOverlayMask><EuiConfirmModal title="Exit maintenance mode" onCancel={() => setExitOpen(false)} onConfirm={() => void exit()} cancelButtonText="Cancel" confirmButtonText="Exit maintenance mode" buttonColor="danger" confirmButtonDisabled={busy} isLoading={busy}>Return this host to normal controller operations.</EuiConfirmModal></EuiOverlayMask>}
    {detail && <EuiOverlayMask><EuiModal onClose={() => setDetail(undefined)} style={{ width: 'min(1000px, 96vw)' }}><EuiModalHeader><EuiModalHeaderTitle>Maintenance plan {detail.plan_id}</EuiModalHeaderTitle></EuiModalHeader><EuiModalBody><MaintenancePlanPreview plan={detail.view as unknown as MaintenancePlanViewModel} formatTimestamp={formatDateTime} /></EuiModalBody><EuiModalFooter><EuiButtonEmpty onClick={() => setDetail(undefined)}>Close</EuiButtonEmpty></EuiModalFooter></EuiModal></EuiOverlayMask>}
  </div>;
}
