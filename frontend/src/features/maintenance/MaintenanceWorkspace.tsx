import { useMemo, useState } from 'react';
import {
  EuiBadge, EuiBasicTable, EuiButton, EuiButtonEmpty, EuiButtonGroup, EuiCallOut, EuiConfirmModal,
  EuiFieldNumber, EuiFieldText, EuiFormRow, EuiHealth, EuiModal, EuiModalBody,
  EuiModalFooter, EuiModalHeader, EuiModalHeaderTitle, EuiOverlayMask, EuiPanel,
  EuiSelect, EuiSpacer, EuiText, EuiTitle,
} from '@elastic/eui';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useConsole } from '../../app-context';
import { hostApi } from '../hosts';
import { formatDateTime } from '../../shared/format';
import { maintenanceApi } from './api';
import {
  MaintenanceOperationActions,
  MaintenanceOperationProgress,
  MaintenancePlanPreview,
  MaintenanceWorkflowActions,
} from './components';
import type {
  MaintenanceOperationAction,
  MaintenanceOperationActionControls,
  MaintenanceOperationProgressModel,
  MaintenancePlanViewModel,
  MaintenanceWorkflowAction,
  MaintenanceWorkflowActionControl,
} from './components';
import type { MaintenanceCapabilities, MaintenancePlanHistoryItem, MaintenancePlanHistoryResponse } from './types';

const stateColor: Record<string, 'success' | 'warning' | 'danger' | 'primary' | 'hollow'> = {
  available: 'success', planning: 'warning', maintenance: 'primary', recovery_required: 'danger',
  ready: 'success', succeeded: 'success', blocked: 'danger', failed: 'danger', cancelled: 'warning', paused: 'warning', executing: 'primary',
};

function idempotencyKey(nodeId: number) {
  const value = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`;
  return `manual-maintenance-${nodeId}-${value}`.slice(0, 128);
}

function previewIdempotencyKey(scope: 'host' | 'container', targetId: number) {
  const value = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`;
  return `maintenance-preview-${scope}-${targetId}-${value}`.slice(0, 128);
}

const terminalPlanStates = new Set(['succeeded', 'failed', 'cancelled']);

interface MaintenancePlanDetail extends Omit<MaintenancePlanHistoryItem, 'view'> {
  run_id?: number | null;
  view: MaintenancePlanViewModel;
  operation?: {
    progress: MaintenanceOperationProgressModel;
    safe_checkpoint: boolean;
    safe_checkpoint_reason: string;
    action_controls: MaintenanceOperationActionControls;
  };
}

function planDetailRefetchInterval(plan?: MaintenancePlanDetail) {
  const state = plan?.operation?.progress.lifecycleState || plan?.lifecycle_state;
  return state && !terminalPlanStates.has(state) ? 3000 : false;
}

type WorkflowScope = 'host_maintenance' | 'container_maintenance';
type WorkflowActionControl = MaintenanceWorkflowActionControl & {
  action: MaintenanceWorkflowAction;
  scope: WorkflowScope;
};

function nextWorkflowAction(
  detail: MaintenancePlanDetail | undefined,
  capabilities: MaintenanceCapabilities | undefined,
): WorkflowActionControl | undefined {
  const progress = detail?.operation?.progress;
  const scope = progress?.workflowScope;
  if (!progress || (scope !== 'host_maintenance' && scope !== 'container_maintenance')) return undefined;

  const workflowState = progress.workflowState
    ?? (progress.lifecycleState === 'ready' ? 'available' : undefined);
  let action: MaintenanceWorkflowAction | undefined;
  let label = '';
  if (workflowState === 'available') {
    action = 'prepare';
    label = scope === 'host_maintenance' ? 'Prepare host' : 'Prepare workload';
  } else if (workflowState === 'ready_to_stop') {
    action = 'stop';
    label = scope === 'host_maintenance' ? 'Stop managed workloads' : 'Stop workload';
  } else if (workflowState === 'maintenance' && scope === 'container_maintenance') {
    action = 'return';
    label = 'Return workload to service';
  } else if (workflowState === 'maintenance' && scope === 'host_maintenance') {
    if (progress.hostBoot.state === 'not_started') {
      action = 'reboot';
      label = 'Reboot host';
    } else if (progress.hostBoot.state === 'returned' || progress.hostBoot.state === 'verified') {
      action = 'return';
      label = 'Return host to service';
    }
  }
  if (!action) return undefined;

  const enabled = scope === 'host_maintenance'
    ? capabilities?.operations.host_reboot === true
    : capabilities?.operations.container_stop === true;
  return {
    action,
    scope,
    enabled,
    label,
    reason: enabled ? undefined : 'This workflow action is disabled until its release capability is approved.',
  };
}

export function MaintenanceWorkspace() {
  const queryClient = useQueryClient();
  const { selectedCluster, selectedClusterId, watchRun } = useConsole();
  const [nodeId, setNodeId] = useState<number>();
  const [enterOpen, setEnterOpen] = useState(false);
  const [exitOpen, setExitOpen] = useState(false);
  const [detailPlanId, setDetailPlanId] = useState<string>();
  const [reason, setReason] = useState('Planned operator maintenance');
  const [duration, setDuration] = useState(3600);
  const [previewScope, setPreviewScope] = useState<'host' | 'container'>('host');
  const [previewAssignmentId, setPreviewAssignmentId] = useState<number>();
  const [previewReason, setPreviewReason] = useState('Planned maintenance');
  const [previewBusy, setPreviewBusy] = useState(false);
  const [detailActionBusy, setDetailActionBusy] = useState<MaintenanceOperationAction>();
  const [workflowActionBusy, setWorkflowActionBusy] = useState<MaintenanceWorkflowAction>();
  const [pendingWorkflowAction, setPendingWorkflowAction] = useState<WorkflowActionControl>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const { data: capabilities } = useQuery({ queryKey: ['maintenance-capabilities'], queryFn: maintenanceApi.capabilities, retry: false });
  const { data: nodes = [] } = useQuery({ queryKey: ['nodes'], queryFn: hostApi.list });
  const clusterNodes = useMemo(() => selectedCluster
    ? nodes.filter((node) => selectedCluster.members.some((member) => member.node_id === node.id))
    : nodes, [nodes, selectedCluster]);
  const selectedNodeId = nodeId || clusterNodes[0]?.id;
  const activeAssignments = useMemo(
    () => (selectedCluster?.assignments || []).filter((assignment) => assignment.state === 'active'),
    [selectedCluster],
  );
  const selectedPreviewAssignmentId = previewAssignmentId || activeAssignments[0]?.id;
  const { data: mode, refetch: refetchMode } = useQuery({
    queryKey: ['manual-maintenance-mode', selectedNodeId], enabled: Boolean(selectedNodeId),
    queryFn: () => maintenanceApi.manualMode(selectedNodeId!),
  });
  const { data: history, isLoading: historyLoading, refetch: refetchHistory } = useQuery<MaintenancePlanHistoryResponse>({
    queryKey: ['maintenance-plans', selectedClusterId],
    queryFn: () => maintenanceApi.listPlans<MaintenancePlanHistoryResponse>({ cluster_id: selectedClusterId, limit: 20 }),
  });
  const detailPlanQuery = useQuery<MaintenancePlanDetail>({
    queryKey: ['maintenance-plan-detail', detailPlanId],
    queryFn: () => maintenanceApi.getPlan(detailPlanId!) as Promise<MaintenancePlanDetail>,
    enabled: Boolean(detailPlanId),
    retry: false,
    refetchInterval: (query) => planDetailRefetchInterval(query.state.data),
  });
  const detail = detailPlanQuery.data;
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
  const createPreview = async () => {
    const targetId = previewScope === 'host' ? selectedNodeId : selectedPreviewAssignmentId;
    if (!targetId || !previewReason.trim()) return;
    setPreviewBusy(true); setError('');
    try {
      const input = previewScope === 'host'
        ? {
          operation: 'host_maintenance', node_id: targetId, reason: previewReason.trim(),
          idempotency_key: previewIdempotencyKey('host', targetId),
        }
        : {
          operation: 'container_maintenance', assignment_id: targetId, reason: previewReason.trim(),
          idempotency_key: previewIdempotencyKey('container', targetId),
        };
      const result = await maintenanceApi.preview<MaintenancePlanHistoryItem>(input);
      setDetailPlanId(result.plan_id); await refetchHistory();
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'Unable to create maintenance preview.'); }
    finally { setPreviewBusy(false); }
  };
  const columns = [
    { field: 'plan_id', name: 'Plan', render: (value: string) => <EuiButtonEmpty size="s" onClick={() => setDetailPlanId(value)}>{value}</EuiButtonEmpty> },
    { field: 'view.header.operation', name: 'Operation', render: (_: unknown, item: MaintenancePlanHistoryItem) => item.view.header.operation },
    { field: 'view.header.target.name', name: 'Target', render: (_: unknown, item: MaintenancePlanHistoryItem) => item.view.header.target.name },
    { field: 'lifecycle_state', name: 'State', render: (value: string) => <EuiBadge color={stateColor[value] || 'hollow'}>{value.replaceAll('_', ' ')}</EuiBadge> },
    { field: 'view.header.createdAt', name: 'Created', render: (_: unknown, item: MaintenancePlanHistoryItem) => formatDateTime(item.view.header.createdAt) },
  ];
  const modeState = mode?.state || 'available';
  const manualEntryEnabled = capabilities?.operations.manual_maintenance_entry === true;
  const manualExitEnabled = capabilities?.lifecycle?.manual_maintenance_exit === true;
  const detailControls = detail?.operation?.action_controls;
  const detailOperationControls = detailControls;
  const workflowControl = nextWorkflowAction(detail, capabilities);
  const runDetailAction = async (action: MaintenanceOperationAction) => {
    if (!detailPlanId || !detailOperationControls?.[action]?.enabled) return;
    setDetailActionBusy(action); setError('');
    try {
      const result = await maintenanceApi.action(detailPlanId, action);
      if (result.run_id) watchRun(result.run_id);
      await Promise.all([detailPlanQuery.refetch(), refetchHistory(), refetchMode()]);
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'Unable to update maintenance recovery state.'); }
    finally { setDetailActionBusy(undefined); }
  };
  const runWorkflowAction = async () => {
    if (!detailPlanId || !pendingWorkflowAction) return;
    const { action, scope } = pendingWorkflowAction;
    setWorkflowActionBusy(action); setError('');
    try {
      const result = scope === 'host_maintenance'
        ? await maintenanceApi.hostWorkflowAction(detailPlanId, action)
        : await maintenanceApi.containerWorkflowAction(detailPlanId, action as 'prepare' | 'stop' | 'return');
      watchRun(result.run_id);
      setPendingWorkflowAction(undefined);
      await Promise.all([detailPlanQuery.refetch(), refetchHistory(), refetchMode()]);
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'Unable to run the maintenance workflow action.'); }
    finally { setWorkflowActionBusy(undefined); }
  };
  return <div className="page-stack">
    <div className="page-heading"><div><EuiTitle><h1>Maintenance</h1></EuiTitle><EuiText color="subdued">Review maintenance plans and manage active maintenance windows.</EuiText></div><EuiButtonEmpty iconType="refresh" onClick={() => void refresh()} isLoading={historyLoading}>Refresh</EuiButtonEmpty></div>
    {!selectedCluster && <EuiCallOut title="Select a cluster" iconType="cluster">Plan history is scoped to the selected cluster. Host maintenance controls remain available for enrolled hosts.</EuiCallOut>}
    {capabilities?.planning === false && <EuiCallOut title="Maintenance planning is disabled" color="warning" iconType="lock">Plan history remains available, but new maintenance previews are disabled.</EuiCallOut>}
    {!manualEntryEnabled && <EuiCallOut title="Starting maintenance is disabled" color="warning" iconType="lock">Existing maintenance windows can still be exited or recovered safely.</EuiCallOut>}
    {error && <EuiCallOut title="Maintenance action failed" color="danger" iconType="warning">{error}</EuiCallOut>}
    <section className="section-band"><div className="section-heading"><EuiTitle size="s"><h2>Plan maintenance</h2></EuiTitle><EuiBadge color={capabilities?.planning ? 'success' : 'hollow'}>{capabilities?.planning ? 'available' : 'disabled'}</EuiBadge></div>
      <EuiPanel hasBorder paddingSize="m"><EuiFormRow label="Target scope"><EuiButtonGroup legend="Maintenance target scope" options={[{ id: 'host', label: 'Host' }, { id: 'container', label: 'Container' }]} idSelected={previewScope} onChange={(id) => setPreviewScope(id as 'host' | 'container')} type="single" /></EuiFormRow><div className="form-grid">
        {previewScope === 'host' ? <EuiFormRow label="Host"><EuiSelect aria-label="Preview host" value={selectedNodeId || ''} onChange={(event) => setNodeId(Number(event.target.value))} options={clusterNodes.length ? clusterNodes.map((node) => ({ value: node.id, text: `${node.name} (${node.address})` })) : [{ value: '', text: 'No eligible hosts' }]} /></EuiFormRow> : <EuiFormRow label="Workload"><EuiSelect aria-label="Maintenance workload" value={selectedPreviewAssignmentId || ''} onChange={(event) => setPreviewAssignmentId(Number(event.target.value))} options={activeAssignments.length ? activeAssignments.map((assignment) => ({ value: assignment.id, text: `${assignment.node_name} / ${assignment.role}` })) : [{ value: '', text: 'No active workloads' }]} /></EuiFormRow>}
        <EuiFormRow label="Reason"><EuiFieldText aria-label="Preview reason" value={previewReason} onChange={(event) => setPreviewReason(event.target.value)} /></EuiFormRow>
      </div><EuiSpacer size="m" /><EuiButton fill iconType="eye" onClick={() => void createPreview()} isLoading={previewBusy} disabled={!capabilities?.planning || !previewReason.trim() || (previewScope === 'host' ? !selectedNodeId : !selectedPreviewAssignmentId)}>Create preview</EuiButton></EuiPanel>
    </section>
    <section className="section-band"><div className="section-heading"><EuiTitle size="s"><h2>Manual maintenance mode</h2></EuiTitle><EuiBadge color={stateColor[modeState] || 'hollow'}>{modeState.replaceAll('_', ' ')}</EuiBadge></div>
      <EuiPanel hasBorder paddingSize="m"><div className="form-grid"><EuiFormRow label="Host"><EuiSelect aria-label="Maintenance host" value={selectedNodeId || ''} onChange={(event) => setNodeId(Number(event.target.value))} options={clusterNodes.length ? clusterNodes.map((node) => ({ value: node.id, text: `${node.name} (${node.address})` })) : [{ value: '', text: 'No eligible hosts' }]} /></EuiFormRow><EuiFormRow label="Active plan"><EuiFieldText readOnly value={mode?.plan_id || 'None'} /></EuiFormRow></div><EuiSpacer size="m" />
      {mode?.expires_at && <EuiText size="s" color="subdued">Window deadline {formatDateTime(mode.expires_at)}. Verified exit or recovery is required.</EuiText>}
      <EuiSpacer size="s" /><EuiButton fill iconType="wrench" onClick={() => { setError(''); setEnterOpen(true); }} disabled={!selectedNodeId || !manualEntryEnabled || modeState !== 'available'}>Enter maintenance mode</EuiButton>{' '}<EuiButton color="danger" iconType="play" onClick={() => { setError(''); setExitOpen(true); }} disabled={!selectedNodeId || !manualExitEnabled || modeState !== 'maintenance'}>Exit maintenance mode</EuiButton></EuiPanel>
    </section>
    <section className="section-band"><div className="section-heading"><EuiTitle size="s"><h2>Plan history</h2></EuiTitle><EuiBadge>{history?.count || 0}</EuiBadge></div><EuiBasicTable items={history?.items || []} columns={columns} loading={historyLoading} noItemsMessage="No maintenance plans match the selected cluster." /></section>
    {enterOpen && <EuiOverlayMask><EuiModal onClose={() => setEnterOpen(false)} initialFocus="[data-autofocus]"><EuiModalHeader><EuiModalHeaderTitle>Enter maintenance mode</EuiModalHeaderTitle></EuiModalHeader><EuiModalBody><EuiText><p>The host remains operator-controlled until you explicitly exit the window or it expires.</p></EuiText><EuiSpacer /><EuiFormRow label="Reason"><EuiFieldText aria-label="Manual maintenance reason" data-autofocus value={reason} onChange={(event) => setReason(event.target.value)} /></EuiFormRow><EuiFormRow label="Duration (seconds)"><EuiFieldNumber min={60} value={duration} onChange={(event) => setDuration(Number(event.target.value))} /></EuiFormRow></EuiModalBody><EuiModalFooter><EuiButtonEmpty onClick={() => setEnterOpen(false)}>Cancel</EuiButtonEmpty><EuiButton fill onClick={() => void enter()} isLoading={busy} disabled={!reason.trim()}>Enter maintenance mode</EuiButton></EuiModalFooter></EuiModal></EuiOverlayMask>}
    {exitOpen && <EuiOverlayMask><EuiConfirmModal title="Exit maintenance mode" onCancel={() => setExitOpen(false)} onConfirm={() => void exit()} cancelButtonText="Cancel" confirmButtonText="Exit maintenance mode" buttonColor="danger" confirmButtonDisabled={busy} isLoading={busy}>Return this host to normal controller operations.</EuiConfirmModal></EuiOverlayMask>}
    {detailPlanId && <EuiOverlayMask><EuiModal onClose={() => setDetailPlanId(undefined)} style={{ width: 'min(1000px, 96vw)' }}><EuiModalHeader><EuiModalHeaderTitle>Maintenance plan {detailPlanId}</EuiModalHeaderTitle></EuiModalHeader><EuiModalBody>
      {detailPlanQuery.isLoading && <EuiText color="subdued">Loading persisted maintenance state.</EuiText>}
      {detailPlanQuery.isError && <EuiCallOut title="Maintenance status refresh failed" color="warning" iconType="warning"><p>{detailPlanQuery.error instanceof Error ? detailPlanQuery.error.message : 'The latest persisted maintenance state could not be loaded.'}</p><EuiButton size="s" iconType="refresh" onClick={() => void detailPlanQuery.refetch()} isLoading={detailPlanQuery.isFetching}>Retry status refresh</EuiButton></EuiCallOut>}
      {detail && <><MaintenancePlanPreview plan={detail.view} formatTimestamp={formatDateTime} />
        {detail.operation && <><EuiSpacer size="m" /><MaintenanceOperationProgress progress={detail.operation.progress} formatTimestamp={formatDateTime} /><EuiSpacer size="m" /><MaintenanceWorkflowActions control={workflowControl} busyAction={workflowActionBusy} onAction={() => { if (workflowControl) setPendingWorkflowAction(workflowControl); }} /><MaintenanceOperationActions lifecycleState={detail.operation.progress.lifecycleState} safeCheckpoint={detail.operation.safe_checkpoint} safeCheckpointReason={detail.operation.safe_checkpoint_reason} controls={detailOperationControls} busyAction={detailActionBusy} onAction={(action) => void runDetailAction(action)} /></>}
      </>}
    </EuiModalBody><EuiModalFooter><EuiButtonEmpty onClick={() => setDetailPlanId(undefined)}>Close</EuiButtonEmpty></EuiModalFooter></EuiModal></EuiOverlayMask>}
    {pendingWorkflowAction && <EuiConfirmModal title={pendingWorkflowAction.label} onCancel={() => setPendingWorkflowAction(undefined)} onConfirm={() => void runWorkflowAction()} cancelButtonText="Cancel" confirmButtonText={pendingWorkflowAction.label} buttonColor={pendingWorkflowAction.action === 'stop' || pendingWorkflowAction.action === 'reboot' ? 'danger' : 'primary'} confirmButtonDisabled={Boolean(workflowActionBusy)} isLoading={Boolean(workflowActionBusy)}>
      {pendingWorkflowAction.action === 'stop' || pendingWorkflowAction.action === 'reboot'
        ? 'This is a planned disruptive action. Continue only after reviewing the persisted maintenance preview and current safety evidence.'
        : 'Run the next approved action for this maintenance workflow.'}
    </EuiConfirmModal>}
  </div>;
}
