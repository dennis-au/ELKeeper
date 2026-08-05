import { useEffect, useMemo, useState } from 'react';
import {
  EuiBadge, EuiButton, EuiButtonEmpty, EuiButtonIcon, EuiCallOut, EuiCodeBlock, EuiDataGrid, EuiFieldText,
  EuiFormRow, EuiModal, EuiModalBody, EuiModalFooter, EuiModalHeader, EuiModalHeaderTitle,
  EuiOverlayMask, EuiSelect, EuiSpacer, EuiText, EuiTextArea, EuiTitle, EuiToolTip,
} from '@elastic/eui';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useConsole } from '../../app-context';
import { clusterApi, type MembershipInput } from '../clusters';
import { hostApi } from '../hosts';
import { runsApi, type RunRecord } from '../runs';
import { versionsApi } from '../versions';
import { workloadsApi } from './index';
import { bytes, roleLabel } from '../../shared/format';
import type { Membership } from '../clusters';
import type { NodeRecord, StorageMountResponse } from '../hosts';
import type { Assignment, TopologyResponse } from './types';
import type { VersionResponse } from '../versions';

type ModalState =
  | { type: 'resources'; assignment: Assignment }
  | { type: 'detach' | 'purge'; assignment: Assignment }
  | { type: 'network'; member: Membership };

type PendingChange =
  | { clientId: string; kind: 'create'; nodeId: number; nodeName: string; role: string; imageVersion: string; config: Record<string, string> }
  | { clientId: string; kind: 'resources'; assignmentId: number; expectedRevision: number; nodeId: number; nodeName: string; role: string; config: Record<string, string>; previousConfig: Record<string, string> }
  | { clientId: string; kind: 'detach'; assignmentId: number; expectedRevision: number; nodeId: number; nodeName: string; role: string };

export const managedWorkloadColumns = [
  { id: 'role', display: 'Role', initialWidth: 170 },
  { id: 'version', display: 'Image version', initialWidth: 140 },
  { id: 'host', display: 'Host', initialWidth: 150 },
  { id: 'state', display: 'Runtime', initialWidth: 140 },
  { id: 'maintenance', display: 'Maintenance', initialWidth: 180 },
  { id: 'resources', display: 'Resources', initialWidth: 180 },
  { id: 'storage', display: 'Storage', initialWidth: 260 },
  { id: 'endpoint', display: 'Endpoint', initialWidth: 240 },
  { id: 'actions', display: 'Actions', initialWidth: 260 },
];

export function workloadImageVersion(assignment: Assignment) {
  if (assignment.observation?.running && assignment.observation.version) return assignment.observation.version;
  return assignment.observation?.version || 'not observed';
}

export function workloadRuntimeVersionLabel(assignment: Assignment) {
  if (assignment.observation?.version) return assignment.observation.version;
  return 'Version not observed';
}

function clientChangeId() {
  return globalThis.crypto?.randomUUID?.() || `change-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function managedStoragePath(mountPoint: string, clusterSlug: string, role: string, nodeId: number) {
  return `${mountPoint.replace(/\/+$/, '')}/elastic-control/${clusterSlug}/${role}-${nodeId}`;
}

function assignmentFormFromPendingChange(change: Extract<PendingChange, { kind: 'create' }>) {
  const { cpu = '2', memory = '4g', storage_path = '', pipeline, jvm_heap, node_heap, ...advanced } = change.config;
  return {
    node_id: change.nodeId,
    role: change.role,
    image_version: change.imageVersion,
    cpu: String(cpu),
    memory: String(memory),
    runtime_heap: String(jvm_heap || node_heap || ''),
    storage_path: String(storage_path),
    pipeline: String(pipeline || 'input { beats { port => 5044 } }\noutput { stdout { codec => rubydebug } }'),
    advanced: JSON.stringify(advanced),
  };
}

function runtimeHeapField(role: string) {
  if (['master', 'hot', 'warm', 'ml', 'ingest', 'coordinating', 'logstash'].includes(role)) {
    return { key: 'jvm_heap', label: 'JVM heap', help: 'Optional. Elasticsearch and Logstash heaps may use at most 50% of the container memory.' };
  }
  if (role === 'kibana') {
    return { key: 'node_heap', label: 'Node.js heap', help: 'Kibana uses Node.js, not a JVM. Optional; up to 75% of the container memory.' };
  }
  return undefined;
}

function configWithRuntimeHeap(config: Record<string, string>, role: string, value: string) {
  const next = { ...config };
  delete next.jvm_heap;
  delete next.node_heap;
  const field = runtimeHeapField(role);
  if (field) next[field.key] = value.trim();
  return next;
}

function pendingChangeSummary(item: PendingChange) {
  if (item.kind === 'create') return `Host: ${item.nodeName} · placement: ${roleLabel(item.role)} · image ${item.imageVersion} · ${item.config.cpu} CPU / ${item.config.memory} / ${item.config.storage_path}`;
  if (item.kind === 'detach') return `Host: ${item.nodeName} · placement released after the reversible changes succeed`;
  const changes = [
    item.previousConfig.cpu !== item.config.cpu && `CPU ${item.previousConfig.cpu} → ${item.config.cpu}`,
    item.previousConfig.memory !== item.config.memory && `Memory ${item.previousConfig.memory} → ${item.config.memory}`,
    item.previousConfig.storage_path !== item.config.storage_path && `Storage ${item.previousConfig.storage_path} → ${item.config.storage_path}`,
  ].filter(Boolean);
  return `Host: ${item.nodeName} · ${changes.join(' · ') || 'No resource difference'}`;
}

function StoragePathPicker({ nodeId, clusterSlug, role, value, onChange, autoFocus = false }: {
  nodeId: number;
  clusterSlug: string;
  role: string;
  value: string;
  onChange: (value: string) => void;
  autoFocus?: boolean;
}) {
  const [selectedMount, setSelectedMount] = useState('');
  const mountsQuery = useQuery({
    queryKey: ['node-storage', nodeId],
    enabled: Boolean(nodeId),
    retry: false,
    staleTime: 15_000,
    queryFn: () => workloadsApi.storage(nodeId),
  });
  useEffect(() => setSelectedMount(''), [nodeId]);
  const mounts = mountsQuery.data?.mounts || [];
  const eligibleMounts = mounts.filter((mount) => mount.eligible);
  const chooseMount = (mountPoint: string) => {
    setSelectedMount(mountPoint);
    onChange(managedStoragePath(mountPoint, clusterSlug, role, nodeId));
  };

  return <div className="storage-path-picker">
    <EuiFormRow label="Host storage mount" helpText="Choose a mounted data device to fill a dedicated workload directory.">
      <div className="storage-path-picker__control">
        <EuiSelect
          aria-label="Host storage mount"
          value={selectedMount}
          disabled={!nodeId || mountsQuery.isLoading || !eligibleMounts.length}
          onChange={(event) => chooseMount(event.target.value)}
          options={[
            { value: '', text: !nodeId ? 'Select a cluster host first' : mountsQuery.isLoading ? 'Inspecting host storage…' : eligibleMounts.length ? 'Select an eligible mount' : 'No eligible mounts found' },
            ...eligibleMounts.map((mount) => ({ value: mount.mount_point, text: `${mount.mount_point} · ${bytes(mount.available_bytes)} free` })),
          ]}
        />
        <EuiToolTip content="Refresh host storage inventory"><EuiButtonIcon aria-label="Refresh host storage inventory" iconType="refresh" onClick={() => mountsQuery.refetch()} isLoading={mountsQuery.isFetching} disabled={!nodeId} /></EuiToolTip>
      </div>
    </EuiFormRow>
    {mountsQuery.isError && <EuiCallOut className="storage-path-picker__error" title="Host storage inventory unavailable" color="danger" size="s">{mountsQuery.error instanceof Error ? mountsQuery.error.message : 'The controller could not inspect this host.'}</EuiCallOut>}
    {Boolean(nodeId && !mountsQuery.isLoading && !mountsQuery.isError) && <div className="storage-device-list" aria-label="Host storage devices">
      {mounts.map((mount) => <div className={`storage-device ${mount.eligible ? 'is-eligible' : ''}`} key={mount.mount_point}>
        <div><strong>{mount.mount_point}</strong><small>{mount.source} · {mount.filesystem} · {bytes(mount.available_bytes)} free of {bytes(mount.size_bytes)}</small></div>
        <EuiBadge color={mount.eligible ? 'success' : 'hollow'}>{mount.eligible ? 'selectable' : mount.unavailable_reason}</EuiBadge>
      </div>)}
      {!mounts.length && <small className="block-muted">No mounted filesystems were reported by the host.</small>}
    </div>}
    <EuiFormRow label="Storage path" helpText="The selected mount fills a dedicated path; manual absolute paths remain supported.">
      <EuiFieldText data-autofocus={autoFocus || undefined} value={value} onChange={(event) => onChange(event.target.value)} />
    </EuiFormRow>
  </div>;
}

function WorkloadModal({ state, close, completed, stageChange }: {
  state: ModalState;
  close: () => void;
  completed: (runId?: number) => Promise<void>;
  stageChange: (kind: 'resources' | 'detach', assignment: Assignment, config?: Record<string, string>) => void;
}) {
  const { selectedCluster } = useConsole();
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState('');
  const resourceAssignment = state.type === 'resources' ? state.assignment : undefined;
  const [cpu, setCpu] = useState(resourceAssignment?.config.cpu || '');
  const [memory, setMemory] = useState(resourceAssignment?.config.memory || '');
  const [runtimeHeap, setRuntimeHeap] = useState(resourceAssignment?.config.jvm_heap || resourceAssignment?.config.node_heap || '');
  const [storage, setStorage] = useState(resourceAssignment?.config.storage_path || '');
  const [network, setNetwork] = useState(state.type === 'network' ? { ...state.member } : undefined);
  const submit = async () => {
    setBusy(true); setError('');
    try {
      if (state.type === 'resources') {
        stageChange('resources', state.assignment, configWithRuntimeHeap({ ...state.assignment.config, cpu, memory, storage_path: storage }, state.assignment.role, runtimeHeap));
      } else if (state.type === 'network' && network) {
        await clusterApi.updateMember(network.cluster_id, network.node_id, {
          node_id: network.node_id,
          network_mode: network.network_mode,
          data_interface: network.data_interface,
          data_address: network.data_address,
          user_interface: network.user_interface,
          user_address: network.user_address,
        });
        await completed();
      } else if (state.type === 'detach') {
        stageChange('detach', state.assignment);
      } else if (state.type === 'purge') {
        const result = await workloadsApi.removeAssignment(state.assignment.id, state.type);
        await completed(result?.run_id);
      } else {
        throw new Error('Network configuration is incomplete');
      }
      close();
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Operation failed'); }
    finally { setBusy(false); }
  };
  const title = state.type === 'resources' ? `Resources: ${state.assignment.role}` : state.type === 'network' ? `Network: ${state.member.name}` : `${state.type === 'purge' ? 'Purge' : 'Detach'} ${state.assignment.role}`;
  const heapField = resourceAssignment && runtimeHeapField(resourceAssignment.role);
  return <EuiOverlayMask><EuiModal onClose={close} initialFocus="[data-autofocus]">
    <EuiModalHeader><EuiModalHeaderTitle>{title}</EuiModalHeaderTitle></EuiModalHeader>
    <EuiModalBody>
      {state.type === 'resources' && <div className="form-grid"><EuiFormRow label="CPU cores"><EuiFieldText data-autofocus value={cpu} onChange={(event) => setCpu(event.target.value)} /></EuiFormRow><EuiFormRow label="Memory"><EuiFieldText value={memory} onChange={(event) => setMemory(event.target.value)} /></EuiFormRow>{heapField && <EuiFormRow label={heapField.label} helpText={heapField.help}><EuiFieldText value={runtimeHeap} placeholder={resourceAssignment.role === 'kibana' ? 'e.g. 12g' : 'Auto / e.g. 8g'} onChange={(event) => setRuntimeHeap(event.target.value)} /></EuiFormRow>}<StoragePathPicker nodeId={state.assignment.node_id} clusterSlug={selectedCluster?.slug || 'cluster'} role={state.assignment.role} value={storage} onChange={setStorage} /></div>}
      {state.type === 'network' && network && <div className="form-grid">
        <EuiFormRow label="Traffic mode"><EuiSelect value={network.network_mode} onChange={(event) => { const mode = event.target.value as Membership['network_mode']; setNetwork({ ...network, network_mode: mode, ...(mode === 'shared' ? { data_interface: network.user_interface, data_address: network.user_address } : {}) }); }} options={[{ value: 'shared', text: 'Shared NIC' }, { value: 'dedicated', text: 'Dedicated NICs' }]} /></EuiFormRow>
        <EuiFormRow label="User NIC"><EuiFieldText data-autofocus value={network.user_interface} onChange={(event) => setNetwork({ ...network, user_interface: event.target.value, ...(network.network_mode === 'shared' ? { data_interface: event.target.value } : {}) })} /></EuiFormRow>
        <EuiFormRow label="User IPv4"><EuiFieldText value={network.user_address} onChange={(event) => setNetwork({ ...network, user_address: event.target.value, ...(network.network_mode === 'shared' ? { data_address: event.target.value } : {}) })} /></EuiFormRow>
        <EuiFormRow label="System/data NIC"><EuiFieldText disabled={network.network_mode === 'shared'} value={network.data_interface} onChange={(event) => setNetwork({ ...network, data_interface: event.target.value })} /></EuiFormRow>
        <EuiFormRow label="System/data IPv4"><EuiFieldText disabled={network.network_mode === 'shared'} value={network.data_address} onChange={(event) => setNetwork({ ...network, data_address: event.target.value })} /></EuiFormRow>
      </div>}
      {state.type === 'detach' && <EuiCallOut title="Detach will be staged" color="warning">The workload stays running until the complete pending change set is applied.</EuiCallOut>}
      {state.type === 'purge' && <><EuiCallOut title="Managed workload and marked data will be removed" color="danger" iconType="warning">Unrelated paths remain protected by the managed marker check.</EuiCallOut><EuiSpacer /><EuiFormRow label="Type PURGE to continue"><EuiFieldText data-autofocus value={confirm} onChange={(event) => setConfirm(event.target.value)} /></EuiFormRow></>}
      {error && <><EuiSpacer /><EuiCallOut title="Operation failed" color="danger">{error}</EuiCallOut></>}
    </EuiModalBody>
    <EuiModalFooter><EuiButtonEmpty onClick={close}>Cancel</EuiButtonEmpty><EuiButton fill color={state.type === 'purge' || state.type === 'detach' ? 'danger' : 'primary'} isLoading={busy} disabled={state.type === 'purge' && confirm !== 'PURGE'} onClick={submit}>{state.type === 'resources' ? 'Stage resources' : state.type === 'network' ? 'Save network' : state.type === 'purge' ? 'Purge workload' : 'Stage detach'}</EuiButton></EuiModalFooter>
  </EuiModal></EuiOverlayMask>;
}

export function RolesWorkspace() {
  const queryClient = useQueryClient();
  const { selectedCluster, watchRun, registerNavigationGuard } = useConsole();
  const { data: nodes = [] } = useQuery({ queryKey: ['nodes'], queryFn: hostApi.list });
  const { data: health } = useQuery({ queryKey: ['health'], queryFn: workloadsApi.roles });
  const { data: topology } = useQuery({ queryKey: ['topology', selectedCluster?.id], enabled: Boolean(selectedCluster), queryFn: () => workloadsApi.topology(selectedCluster!.id) });
  const [member, setMember] = useState<MembershipInput>({ node_id: 0, network_mode: 'shared', user_interface: 'ens18', user_address: '', data_interface: 'ens18', data_address: '' });
  const [assignment, setAssignment] = useState({ node_id: 0, role: 'master', image_version: '', cpu: '2', memory: '4g', runtime_heap: '', storage_path: '', pipeline: 'input { beats { port => 5044 } }\noutput { stdout { codec => rubydebug } }', advanced: '{}' });
  const [pendingChanges, setPendingChanges] = useState<PendingChange[]>([]);
  const [applyingRunId, setApplyingRunId] = useState<number>();
  const [modal, setModal] = useState<ModalState>();
  const [error, setError] = useState('');
  const [visibleColumns, setVisibleColumns] = useState(() => managedWorkloadColumns.map((column) => column.id));
  const [editingChangeId, setEditingChangeId] = useState<string>();
  const [pendingNavigation, setPendingNavigation] = useState<(() => void)>();
  const roles = health?.roles || [];
  const versionsQuery = useQuery({
    queryKey: ['versions', selectedCluster?.id, 'role', assignment.role],
    enabled: Boolean(selectedCluster && assignment.role),
    queryFn: () => versionsApi.list(selectedCluster!.id, assignment.role),
  });
  const imageVersions = useMemo(() => {
    const recommended = versionsQuery.data?.recommended_version || selectedCluster?.desired_version || '';
    return Array.from(new Set([...(versionsQuery.data?.available_versions || []), recommended].filter(Boolean)));
  }, [selectedCluster?.desired_version, versionsQuery.data]);
  const availableNodes = nodes.filter((node) => node.enabled && !selectedCluster?.members.some((item) => item.node_id === node.id));
  const refresh = async (runId?: number) => { if (runId) watchRun(runId); await queryClient.invalidateQueries(); };
  const { data: runs = [] } = useQuery({ queryKey: ['runs'], queryFn: runsApi.list, enabled: Boolean(applyingRunId), refetchInterval: applyingRunId ? 2000 : false });

  useEffect(() => {
    if (!versionsQuery.data || editingChangeId) return;
    const recommended = versionsQuery.data.recommended_version || imageVersions[0] || '';
    setAssignment((current) => current.image_version === recommended ? current : { ...current, image_version: recommended });
  }, [editingChangeId, imageVersions, versionsQuery.data]);

  useEffect(() => {
    const run = runs.find((item: RunRecord) => item.id === applyingRunId);
    if (!run || ['queued', 'running'].includes(run.status)) return;
    if (run.status === 'succeeded') {
      setPendingChanges([]);
      setError('');
      void refresh();
    } else {
      setError('Pending workload changes were rolled back. Correct the staged changes and apply them again.');
    }
    setApplyingRunId(undefined);
  }, [applyingRunId, runs]);

  const hasPendingChanges = Boolean(pendingChanges.length);
  useEffect(() => {
    if (!registerNavigationGuard) return;
    if (!hasPendingChanges) {
      registerNavigationGuard(undefined);
      return;
    }
    registerNavigationGuard((continueNavigation) => {
      setPendingNavigation(() => continueNavigation);
      return true;
    });
    return () => registerNavigationGuard(undefined);
  }, [hasPendingChanges, registerNavigationGuard]);

  useEffect(() => {
    if (!hasPendingChanges) return;
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = '';
    };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [hasPendingChanges]);

  const addMember = async (event: React.FormEvent) => {
    event.preventDefault(); if (!selectedCluster) return;
    try {
      await clusterApi.addMember(selectedCluster.id, {
        ...member,
        node_id: Number(member.node_id),
        data_interface: member.network_mode === 'shared' ? member.user_interface : member.data_interface,
        data_address: member.network_mode === 'shared' ? member.user_address : member.data_address,
      });
      setMember({ node_id: 0, network_mode: selectedCluster.network_defaults.mode, user_interface: 'ens18', user_address: '', data_interface: 'ens18', data_address: '' }); await refresh();
    } catch (reason) { setError((reason as Error).message); }
  };
  const removeMember = async (item: Membership) => {
    try { await clusterApi.removeMember(item.cluster_id, item.node_id); await refresh(); }
    catch (reason) { setError((reason as Error).message); }
  };
  const addAssignment = async (event: React.FormEvent) => {
    event.preventDefault(); if (!selectedCluster) return;
    try {
      const advanced = JSON.parse(assignment.advanced || '{}');
      const node = selectedCluster.members.find((item) => item.node_id === Number(assignment.node_id));
      if (!node) throw new Error('Choose a host in this cluster');
      if (pendingChanges.some((item) => item.kind === 'create' && item.clientId !== editingChangeId && item.nodeId === node.node_id && item.role === assignment.role)) {
        throw new Error('This role is already staged on the selected host');
      }
      if (selectedCluster.assignments.some((item) => item.node_id === node.node_id && item.role === assignment.role)) {
        throw new Error('This role is already managed on the selected host');
      }
      const change: Extract<PendingChange, { kind: 'create' }> = {
        clientId: clientChangeId(), kind: 'create', nodeId: node.node_id, nodeName: node.name, role: assignment.role,
        imageVersion: assignment.image_version,
        config: configWithRuntimeHeap({ ...advanced, cpu: assignment.cpu, memory: assignment.memory, storage_path: assignment.storage_path, ...(assignment.role === 'logstash' ? { pipeline: assignment.pipeline } : {}) }, assignment.role, assignment.runtime_heap),
      };
      setPendingChanges((current) => editingChangeId
        ? current.map((item) => item.clientId === editingChangeId ? { ...change, clientId: editingChangeId } : item)
        : [...current, change]);
      setEditingChangeId(undefined);
      setAssignment({ ...assignment, storage_path: '', advanced: '{}' });
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Invalid assignment configuration'); }
  };
  const stageChange = (kind: 'resources' | 'detach', item: Assignment, config?: Record<string, string>) => {
    setPendingChanges((current) => {
      const existing = current.find((change) => 'assignmentId' in change && change.assignmentId === item.id);
      const next = kind === 'resources'
        ? { clientId: clientChangeId(), kind, assignmentId: item.id, expectedRevision: item.revision, nodeId: item.node_id, nodeName: item.node_name, role: item.role, config: config || item.config, previousConfig: existing?.kind === 'resources' ? existing.previousConfig : item.config } as PendingChange
        : { clientId: clientChangeId(), kind, assignmentId: item.id, expectedRevision: item.revision, nodeId: item.node_id, nodeName: item.node_name, role: item.role } as PendingChange;
      return [...current.filter((change) => !('assignmentId' in change && change.assignmentId === item.id)), next];
    });
  };
  const removePendingChange = (clientId: string) => {
    if (editingChangeId === clientId) setEditingChangeId(undefined);
    setPendingChanges((current) => current.filter((item) => item.clientId !== clientId));
  };
  const editPendingCreate = (change: Extract<PendingChange, { kind: 'create' }>) => {
    setAssignment(assignmentFormFromPendingChange(change));
    setEditingChangeId(change.clientId);
  };
  const discardPendingChanges = () => {
    setPendingChanges([]);
    setEditingChangeId(undefined);
  };
  const discardPendingChangesAndContinue = () => {
    const continueNavigation = pendingNavigation;
    setPendingNavigation(undefined);
    discardPendingChanges();
    continueNavigation?.();
  };
  const applyPendingChanges = async () => {
    if (!selectedCluster || !pendingChanges.length) return;
    setError('');
    try {
      const result = await workloadsApi.applyChanges(selectedCluster.id, { changes: pendingChanges.map((item) => item.kind === 'create'
          ? { client_id: item.clientId, kind: item.kind, node_id: item.nodeId, role: item.role, image_version: item.imageVersion, config: item.config }
          : item.kind === 'resources'
            ? { client_id: item.clientId, kind: item.kind, assignment_id: item.assignmentId, expected_revision: item.expectedRevision, config: item.config }
            : { client_id: item.clientId, kind: item.kind, assignment_id: item.assignmentId, expected_revision: item.expectedRevision }) });
      setApplyingRunId(result.run_id);
      watchRun(result.run_id);
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Unable to apply pending workload changes'); }
  };
  const rows = selectedCluster?.assignments || [];
  const stageReady = Boolean(assignment.node_id && assignment.image_version && assignment.storage_path && !versionsQuery.isLoading && !applyingRunId);
  const stageStatus = !assignment.node_id ? 'Host required' : !assignment.image_version || versionsQuery.isLoading ? 'Version required' : !assignment.storage_path ? 'Storage required' : 'Ready to stage';
  const access = new Map((topology?.access_urls || []).map((item) => [item.assignment_id, item.url]));
  const renderCellValue = ({ rowIndex, columnId }: { rowIndex: number; columnId: string }) => {
    const item = rows[rowIndex];
    if (!item) return null;
    if (columnId === 'role') return <strong>{roleLabel(item.role)}</strong>;
    if (columnId === 'version') return <span className="image-version-value" title={item.observation?.image}>{workloadImageVersion(item)}</span>;
    if (columnId === 'host') return item.node_name;
    if (columnId === 'state') return <EuiBadge color={item.observation?.running ? 'success' : 'default'}>{item.observation?.running ? 'running' : item.state}</EuiBadge>;
    if (columnId === 'maintenance') {
      const progress = item.maintenance;
      if (!progress) return <span>none</span>;
      const recovery = progress.checkpoint?.recovery_classification;
      const color = progress.lifecycle_state === 'recovery_required' || recovery === 'recovery_required' ? 'danger' : progress.lifecycle_state === 'blocked' ? 'warning' : 'primary';
      return <EuiToolTip content={`Plan ${progress.plan_id}: ${progress.verified_steps}/${progress.step_count} verified`}><EuiBadge color={color}>{recovery || progress.lifecycle_state}</EuiBadge></EuiToolTip>;
    }
    if (columnId === 'resources') return `${item.config.cpu} CPU · ${item.config.memory}`;
    if (columnId === 'storage') return <span title={item.config.storage_path}>{item.config.storage_path}</span>;
    if (columnId === 'endpoint') return access.get(item.id) || 'Outbound/no listener';
    if (columnId === 'actions') {
      const change = pendingChanges.find((pending) => 'assignmentId' in pending && pending.assignmentId === item.id);
      const stagedAssignment = change?.kind === 'resources' ? { ...item, config: change.config } : item;
      return <div className="grid-actions"><EuiButtonEmpty size="s" disabled={Boolean(applyingRunId)} onClick={() => setModal({ type: 'resources', assignment: stagedAssignment })}>{change?.kind === 'resources' ? 'Edit resources' : 'Resources'}</EuiButtonEmpty><EuiButtonEmpty size="s" disabled={Boolean(applyingRunId)} onClick={() => setModal({ type: 'detach', assignment: item })}>{change?.kind === 'detach' ? 'Edit detach' : 'Detach'}</EuiButtonEmpty><EuiButtonEmpty size="s" color="danger" disabled={Boolean(change) || Boolean(applyingRunId)} onClick={() => setModal({ type: 'purge', assignment: item })}>Purge</EuiButtonEmpty></div>;
    }
    return null;
  };
  const matrix = useMemo(() => selectedCluster?.members.map((host) => ({
    host,
    assigned: new Map<string, Assignment>(selectedCluster.assignments.filter((item) => item.node_id === host.node_id).map((item) => [item.role, item])),
    creating: new Set(pendingChanges.filter((item) => item.kind === 'create' && item.nodeId === host.node_id).map((item) => item.role)),
    detaching: new Set(pendingChanges.filter((item) => item.kind === 'detach' && item.nodeId === host.node_id).map((item) => item.role)),
  })) || [], [selectedCluster, pendingChanges]);

  if (!selectedCluster) return <div className="page-stack">
    <div className="page-heading"><div><EuiTitle><h1>Workload Placement</h1></EuiTitle><EuiText color="subdued">Map cluster workloads to initialized Podman hosts and manage their persistent limits.</EuiText></div></div>
    <EuiCallOut title="Select or create a cluster" iconType="cluster" />
  </div>;
  return <div className="page-stack">
    <div className="page-heading"><div><EuiTitle><h1>Workload Placement</h1></EuiTitle><EuiText color="subdued">Map cluster workloads to initialized Podman hosts and manage their persistent limits.</EuiText></div><EuiBadge color="hollow">{selectedCluster.name}</EuiBadge></div>
    {error && <EuiCallOut title="Role operation failed" color="danger" iconType="warning">{error}</EuiCallOut>}
    <section className="section-band">
      <div className="section-heading"><div><EuiTitle size="s"><h2>Host membership</h2></EuiTitle><EuiText color="subdued">Each membership declares system/data and user-facing network bindings.</EuiText></div></div>
      <form className="form-grid role-form" onSubmit={addMember}>
        <EuiFormRow label="Host"><EuiSelect value={member.node_id} onChange={(event) => setMember({ ...member, node_id: Number(event.target.value) })} options={[{ value: 0, text: 'Select host' }, ...availableNodes.map((node: NodeRecord) => ({ value: node.id, text: `${node.name} · ${node.address}` }))]} /></EuiFormRow>
        <EuiFormRow label="Traffic mode"><EuiSelect value={member.network_mode} onChange={(event) => setMember({ ...member, network_mode: event.target.value as MembershipInput['network_mode'] })} options={[{ value: 'shared', text: 'Shared NIC' }, { value: 'dedicated', text: 'Dedicated NICs' }]} /></EuiFormRow>
        <EuiFormRow label="User NIC"><EuiFieldText value={member.user_interface} onChange={(event) => setMember({ ...member, user_interface: event.target.value })} /></EuiFormRow>
        <EuiFormRow label="User IPv4"><EuiFieldText value={member.user_address} onChange={(event) => setMember({ ...member, user_address: event.target.value })} /></EuiFormRow>
        {member.network_mode === 'dedicated' && <><EuiFormRow label="System/data NIC"><EuiFieldText value={member.data_interface} onChange={(event) => setMember({ ...member, data_interface: event.target.value })} /></EuiFormRow><EuiFormRow label="System/data IPv4"><EuiFieldText value={member.data_address} onChange={(event) => setMember({ ...member, data_address: event.target.value })} /></EuiFormRow></>}
        <div className="form-submit"><EuiButton type="submit" fill disabled={!member.node_id}>Add host to cluster</EuiButton></div>
      </form>
      <div className="membership-list">{selectedCluster.members.map((item) => <div key={item.node_id}><span><strong>{item.name}</strong><small>zone {item.zone_id || 'not assigned'} · {item.network_mode} · user {item.user_interface} {item.user_address} · data {item.data_interface} {item.data_address}</small></span><EuiBadge color={item.network_ready ? 'success' : 'warning'}>{item.network_ready ? 'ready' : 'incomplete'}</EuiBadge><EuiButtonEmpty size="s" onClick={() => setModal({ type: 'network', member: item })}>Edit network</EuiButtonEmpty><EuiButtonEmpty size="s" color="danger" onClick={() => removeMember(item)}>Remove</EuiButtonEmpty></div>)}</div>
    </section>
    <section className="section-band">
      <div className="section-heading"><div><EuiTitle size="s"><h2>Cluster → roles → hosts</h2></EuiTitle><EuiText color="subdued">Solid cells are managed workloads; outlined cells are pending changes.</EuiText></div></div>
      <div className="role-matrix" aria-label="Cluster workload placement matrix"><div className="role-matrix__header">Host</div>{roles.map((role) => <div className="role-matrix__header" key={role.id}>{role.label}</div>)}{matrix.flatMap(({ host, assigned, creating, detaching }) => [<div className="role-matrix__host" key={`${host.node_id}-host`}><strong>{host.name}</strong><small className="block-muted">{host.zone_id || 'no zone'}</small></div>, ...roles.map((role) => {
        const isCreating = creating.has(role.id);
        const managedAssignment = assigned.get(role.id);
        const isAssigned = Boolean(managedAssignment);
        const isDetaching = detaching.has(role.id);
        return <div key={`${host.node_id}-${role.id}`} className={`role-cell ${isAssigned ? 'is-assigned' : ''} ${isCreating ? 'is-pending' : ''} ${isDetaching ? 'is-pending-removal' : ''}`}>{isCreating ? `${role.label} pending` : isDetaching ? `${role.label} detach` : managedAssignment ? <><strong>{role.label}</strong><small className="role-cell__version">{workloadRuntimeVersionLabel(managedAssignment)}</small></> : '—'}</div>;
      })])}</div>
    </section>
    <section className="section-band workload-stage-band">
      <div className="section-heading"><div><EuiTitle size="s"><h2>{editingChangeId ? 'Edit staged workload' : 'Stage workload'}</h2></EuiTitle><EuiText color="subdued">Prepare one workload placement before adding it to the pending change set.</EuiText></div>{editingChangeId && <EuiBadge color="warning">Editing pending change</EuiBadge>}</div>
      <form className="workload-stage-form" onSubmit={addAssignment}>
        <div className="workload-stage-top-grid">
          <div className="workload-stage-group workload-stage-group--placement">
            <EuiTitle size="xxs"><h3>Placement</h3></EuiTitle>
            <div className="workload-stage-grid workload-stage-grid--placement">
              <EuiFormRow label="Host"><EuiSelect value={assignment.node_id} onChange={(event) => setAssignment({ ...assignment, node_id: Number(event.target.value), storage_path: '' })} options={[{ value: 0, text: 'Select cluster host' }, ...selectedCluster.members.map((item) => ({ value: item.node_id, text: item.name }))]} /></EuiFormRow>
              <EuiFormRow label="Role"><EuiSelect value={assignment.role} onChange={(event) => setAssignment({ ...assignment, role: event.target.value, image_version: '', runtime_heap: '', storage_path: '' })} options={roles.map((role) => ({ value: role.id, text: role.label }))} /></EuiFormRow>
              <EuiFormRow label="Image version" helpText={versionsQuery.data?.registry_error || 'Current cluster version is selected when available.'}><EuiSelect isLoading={versionsQuery.isLoading} value={assignment.image_version} onChange={(event) => setAssignment({ ...assignment, image_version: event.target.value })} options={imageVersions.length ? imageVersions.map((version) => ({ value: version, text: version })) : [{ value: '', text: versionsQuery.isLoading ? 'Loading versions' : 'No versions available' }]} /></EuiFormRow>
            </div>
          </div>
          <div className="workload-stage-group workload-stage-group--resources">
            <EuiTitle size="xxs"><h3>Resources</h3></EuiTitle>
            <div className="workload-stage-grid workload-stage-grid--resources">
              <EuiFormRow label="CPU cores"><EuiFieldText value={assignment.cpu} onChange={(event) => setAssignment({ ...assignment, cpu: event.target.value })} /></EuiFormRow>
              <EuiFormRow label="Memory"><EuiFieldText value={assignment.memory} onChange={(event) => setAssignment({ ...assignment, memory: event.target.value })} /></EuiFormRow>
              {runtimeHeapField(assignment.role) && <EuiFormRow label={runtimeHeapField(assignment.role)?.label} helpText={runtimeHeapField(assignment.role)?.help}><EuiFieldText value={assignment.runtime_heap} placeholder={assignment.role === 'kibana' ? 'e.g. 12g' : 'Auto / e.g. 8g'} onChange={(event) => setAssignment({ ...assignment, runtime_heap: event.target.value })} /></EuiFormRow>}
            </div>
          </div>
        </div>
        <div className="workload-stage-detail-grid">
          <div className="workload-stage-group workload-stage-group--storage">
            <EuiTitle size="xxs"><h3>Storage</h3></EuiTitle>
            <StoragePathPicker nodeId={assignment.node_id} clusterSlug={selectedCluster.slug} role={assignment.role} value={assignment.storage_path} onChange={(storage_path) => setAssignment({ ...assignment, storage_path })} />
          </div>
          <div className="workload-stage-group workload-stage-group--advanced">
            <EuiTitle size="xxs"><h3>Advanced configuration</h3></EuiTitle>
            {assignment.role === 'logstash' && <EuiFormRow label="Pipeline"><EuiTextArea value={assignment.pipeline} onChange={(event) => setAssignment({ ...assignment, pipeline: event.target.value })} /></EuiFormRow>}
            <EuiFormRow label="Advanced JSON"><EuiTextArea value={assignment.advanced} onChange={(event) => setAssignment({ ...assignment, advanced: event.target.value })} /></EuiFormRow>
          </div>
        </div>
        <div className="workload-stage-footer">
          <div className="workload-stage-status" aria-live="polite"><EuiBadge color={stageReady ? 'success' : 'hollow'}>{stageStatus}</EuiBadge></div>
          <div className="workload-stage-actions">{editingChangeId && <EuiButtonEmpty iconType="cross" onClick={() => setEditingChangeId(undefined)}>Cancel edit</EuiButtonEmpty>}<EuiButton type="submit" fill iconType={editingChangeId ? 'check' : 'plusInCircle'} disabled={!stageReady}>{editingChangeId ? 'Update pending change' : 'Add to pending changes'}</EuiButton></div>
        </div>
      </form>
    </section>
    {pendingChanges.length > 0 && <section className="section-band pending-changes-band">
      <div className="section-heading"><div><EuiTitle size="s"><h2>Pending changes</h2></EuiTitle><EuiText color="subdued">{applyingRunId ? 'Applying the complete workload change set.' : `${pendingChanges.length} change${pendingChanges.length === 1 ? '' : 's'} staged in this browser.`}</EuiText></div><div className="pending-changes-band__actions"><EuiButtonEmpty color="danger" disabled={Boolean(applyingRunId)} onClick={discardPendingChanges}>Discard all</EuiButtonEmpty><EuiButton fill isLoading={Boolean(applyingRunId)} disabled={Boolean(applyingRunId)} onClick={applyPendingChanges}>Apply {pendingChanges.length} change{pendingChanges.length === 1 ? '' : 's'}</EuiButton></div></div>
      <div className="pending-change-list">{pendingChanges.map((item) => <div key={item.clientId} className="pending-change-row"><div><strong>{item.kind === 'create' ? 'Create' : item.kind === 'resources' ? 'Update resources' : 'Detach'} {roleLabel(item.role)}</strong><small>{pendingChangeSummary(item)}</small></div><EuiBadge color={applyingRunId ? 'primary' : 'warning'}>{applyingRunId ? 'applying' : 'pending'}</EuiBadge>{item.kind === 'create' && <EuiButtonIcon aria-label={`Edit pending workload ${item.role} on ${item.nodeName}`} iconType="pencil" disabled={Boolean(applyingRunId)} onClick={() => editPendingCreate(item)} />}<EuiButtonIcon aria-label={`Remove ${item.clientId} from pending changes`} iconType="cross" color="danger" disabled={Boolean(applyingRunId)} onClick={() => removePendingChange(item.clientId)} /></div>)}</div>
    </section>}
    <section className="section-band data-grid-band">
      <div className="section-heading"><div><EuiTitle size="s"><h2>Managed workloads</h2></EuiTitle><EuiText color="subdued">Only workloads from a successful change set are managed here.</EuiText></div></div>
      <EuiDataGrid aria-label="Managed workloads" columns={managedWorkloadColumns} columnVisibility={{ visibleColumns, setVisibleColumns }} rowCount={rows.length} renderCellValue={renderCellValue} gridStyle={{ border: 'horizontal', rowHover: 'highlight' }} toolbarVisibility={{ showColumnSelector: true, showFullScreenSelector: false, showDisplaySelector: false }} />
    </section>
    <section className="section-band">
      <div className="section-heading"><div><EuiTitle size="s"><h2>Terminal topology</h2></EuiTitle><EuiText color="subdued">Configured access URLs and role boxes use user/data NIC bindings.</EuiText></div></div>
      <EuiCodeBlock language="text" fontSize="s" paddingSize="m" overflowHeight={520} isCopyable>{topology?.topology || 'Topology is not available.'}</EuiCodeBlock>
    </section>
    {modal && <WorkloadModal state={modal} close={() => setModal(undefined)} completed={refresh} stageChange={stageChange} />}
    {pendingNavigation && <EuiOverlayMask><EuiModal onClose={() => setPendingNavigation(undefined)}>
      <EuiModalHeader><EuiModalHeaderTitle>{applyingRunId ? 'Workload changes are applying' : 'Discard pending workload changes?'}</EuiModalHeaderTitle></EuiModalHeader>
      <EuiModalBody><EuiText>{applyingRunId ? 'The current batch must finish or roll back before leaving Workload Placement.' : 'These browser-local changes have not been applied. Discarding them cannot be undone.'}</EuiText></EuiModalBody>
      <EuiModalFooter><EuiButtonEmpty onClick={() => setPendingNavigation(undefined)}>{applyingRunId ? 'Keep watching' : 'Keep changes'}</EuiButtonEmpty>{!applyingRunId && <EuiButton fill color="danger" onClick={discardPendingChangesAndContinue}>Discard changes</EuiButton>}</EuiModalFooter>
    </EuiModal></EuiOverlayMask>}
  </div>;
}
