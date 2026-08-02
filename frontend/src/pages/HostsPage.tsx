import { useState } from 'react';
import {
  EuiBadge, EuiBasicTable, EuiButton, EuiButtonEmpty, EuiCallOut, EuiCheckbox, EuiConfirmModal,
  EuiFieldNumber, EuiFieldPassword, EuiFieldText, EuiForm, EuiFormRow, EuiHealth, EuiModal,
  EuiIcon, EuiModalBody, EuiModalFooter, EuiModalHeader, EuiModalHeaderTitle, EuiOverlayMask, EuiPanel,
  EuiRadioGroup, EuiSelect, EuiSpacer, EuiSwitch, EuiText, EuiTitle,
} from '@elastic/eui';
import type { EuiBasicTableColumn } from '@elastic/eui';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api, jsonBody, queries } from '../api';
import { useConsole } from '../app-context';
import { timeAgo } from '../format';
import type { Cluster, HostRuntime, NodeRecord } from '../types';

function isIpAddress(value: string) {
  const address = value.trim();
  const octets = address.split('.');
  if (octets.length === 4 && octets.every((octet) => /^\d{1,3}$/.test(octet) && Number(octet) <= 255)) return true;
  return address.includes(':') && /^[0-9A-Fa-f:.]+$/.test(address);
}

function HostEditor({ node, cluster, onClose, onRun }: { node?: NodeRecord; cluster?: Cluster; onClose: () => void; onRun: (runId: number) => void }) {
  const queryClient = useQueryClient();
  const [form, setForm] = useState({ name: node?.name || '', address: node?.address || '', ssh_user: node?.ssh_user || 'root', ssh_port: node?.ssh_port || 22, enabled: node?.enabled ?? true, ssh_host_key: node?.ssh_host_key || '', auth_method: 'controller_key', password: '', install_controller_key: true, zone_id: node?.zone_id || cluster?.zoning?.zones[0] || '' });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [testingPassword, setTestingPassword] = useState(false);
  const [passwordTest, setPasswordTest] = useState<{ authenticated: boolean; message: string }>();
  const testPassword = async () => {
    if (!form.password || !isIpAddress(form.address)) return;
    setTestingPassword(true); setPasswordTest(undefined);
    try {
      const result = await api<{ authenticated: boolean; message: string }>('/api/nodes/test-password', { method: 'POST', ...jsonBody({
        address: form.address, ssh_user: form.ssh_user, ssh_port: form.ssh_port, ssh_host_key: form.ssh_host_key, password: form.password,
      }) });
      setPasswordTest(result);
    } catch (reason) { setPasswordTest({ authenticated: false, message: reason instanceof Error ? reason.message : 'Password authentication test failed.' }); }
    finally { setTestingPassword(false); }
  };
  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!isIpAddress(form.address)) { setError('Enter an IPv4 or IPv6 address. DNS hostnames are not accepted.'); return; }
    setBusy(true); setError('');
    try {
      if (node) await api(`/api/nodes/${node.id}`, { method: 'PUT', ...jsonBody({ name: form.name, address: form.address, ssh_user: form.ssh_user, ssh_port: form.ssh_port, enabled: form.enabled, ssh_host_key: form.ssh_host_key }) });
      else {
        const result = await api<{ run_id: number }>('/api/nodes/enroll', { method: 'POST', ...jsonBody({
          name: form.name, address: form.address, ssh_user: form.ssh_user, ssh_port: form.ssh_port, enabled: form.enabled,
          ssh_host_key: form.ssh_host_key, auth_method: form.auth_method,
          password: form.auth_method === 'password' ? form.password : undefined,
          install_controller_key: form.install_controller_key,
          zone_id: form.zone_id || undefined,
          zone_cluster_id: form.zone_id ? cluster?.id : undefined,
        }) });
        onRun(result.run_id);
      }
      await queryClient.invalidateQueries({ queryKey: ['nodes'] }); onClose();
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Unable to save host'); }
    finally { setBusy(false); }
  };
  return (
    <EuiPanel hasBorder paddingSize="l">
      <EuiForm component="form" onSubmit={save} isInvalid={Boolean(error)} error={error ? [error] : undefined}>
        <EuiTitle size="s"><h2>{node ? 'Edit host' : 'Add host'}</h2></EuiTitle><EuiSpacer />
        <div className="form-grid">
          <EuiFormRow label="Inventory name" helpText={node ? undefined : 'Optional. ELKeeper uses the remote hostname after successful enrollment when this is blank.'}><EuiFieldText value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} /></EuiFormRow>
          <EuiFormRow label="SSH IP address" helpText="IPv4 or IPv6 literal only. DNS hostnames are rejected." isInvalid={Boolean(form.address) && !isIpAddress(form.address)} error={Boolean(form.address) && !isIpAddress(form.address) ? 'Enter a valid IP address.' : undefined}><EuiFieldText value={form.address} onChange={(event) => setForm({ ...form, address: event.target.value })} autoComplete="off" spellCheck={false} /></EuiFormRow>
          <EuiFormRow label="SSH user"><EuiFieldText value={form.ssh_user} onChange={(event) => setForm({ ...form, ssh_user: event.target.value })} /></EuiFormRow>
          <EuiFormRow label="SSH port"><EuiFieldNumber min={1} max={65535} value={form.ssh_port} onChange={(event) => setForm({ ...form, ssh_port: Number(event.target.value) })} /></EuiFormRow>
          {!node && <EuiFormRow label="Zone" helpText={cluster ? `Zones defined by ${cluster.name}.` : 'Select a cluster first to assign a host zone.'}><EuiSelect value={form.zone_id} disabled={!cluster?.zoning?.zones.length} onChange={(event) => setForm({ ...form, zone_id: event.target.value })} options={[{ value: '', text: cluster?.zoning?.zones.length ? 'Select zone' : 'No zones defined' }, ...(cluster?.zoning?.zones || []).map((zone) => ({ value: zone, text: zone }))]} /></EuiFormRow>}
          <EuiFormRow><EuiCheckbox id="host-enabled" label="Enabled for controller operations" checked={form.enabled} onChange={(event) => setForm({ ...form, enabled: event.target.checked })} /></EuiFormRow>
        </div>
        {node && <><EuiSpacer size="s" /><EuiFormRow label="SSH host public key (optional)" helpText="Leave blank to skip host-key validation. A supplied OpenSSH host key remains pinned and replacement is audited."><EuiFieldText value={form.ssh_host_key} onChange={(event) => setForm({ ...form, ssh_host_key: event.target.value })} placeholder="ssh-ed25519 AAAA..." /></EuiFormRow></>}
        {!node && <>
          <EuiSpacer />
          <EuiFormRow label="SSH host public key (optional)" helpText="Leave blank to skip host-key validation. When supplied, ELKeeper pins this exact OpenSSH host key."><EuiFieldText value={form.ssh_host_key} onChange={(event) => setForm({ ...form, ssh_host_key: event.target.value })} placeholder="ssh-ed25519 AAAA..." /></EuiFormRow>
          <EuiFormRow label="Authentication setup"><EuiRadioGroup options={[{ id: 'host-auth-key', label: 'Use the configured controller key', value: 'controller_key' }, { id: 'host-auth-password', label: 'Bootstrap with a one-time password', value: 'password' }]} idSelected={form.auth_method === 'password' ? 'host-auth-password' : 'host-auth-key'} onChange={(id) => setForm({ ...form, auth_method: id === 'host-auth-password' ? 'password' : 'controller_key', password: '' })} /></EuiFormRow>
          {form.auth_method === 'password' && <><EuiSpacer size="s" /><EuiFormRow label="Host password" helpText="Used only for this enrollment run and then discarded."><EuiFieldPassword value={form.password} onChange={(event) => { setForm({ ...form, password: event.target.value }); setPasswordTest(undefined); }} autoComplete="new-password" /></EuiFormRow><EuiButton type="button" onClick={testPassword} isLoading={testingPassword} disabled={!form.password || !isIpAddress(form.address)}>Test password</EuiButton>{passwordTest && <><EuiSpacer size="s" /><EuiCallOut size="s" color={passwordTest.authenticated ? 'success' : 'danger'} title={passwordTest.authenticated ? 'SSH password verified' : 'SSH password test failed'}><p>{passwordTest.message}</p></EuiCallOut></>}<EuiFormRow><EuiCheckbox id="install-controller-key" label="Install and use the controller SSH key for future connections" checked={form.install_controller_key} onChange={(event) => setForm({ ...form, install_controller_key: event.target.checked })} /></EuiFormRow></>}
        </>}
        <EuiSpacer /><div className="form-actions"><EuiButtonEmpty onClick={onClose}>Cancel</EuiButtonEmpty><EuiButton fill type="submit" isLoading={busy}>Save host</EuiButton></div>
      </EuiForm>
    </EuiPanel>
  );
}

export function HostsPage() {
  const queryClient = useQueryClient();
  const { watchRun, clusters, selectedCluster } = useConsole();
  const { data: nodes = [] } = useQuery({ queryKey: ['nodes'], queryFn: queries.nodes });
  const { data: dashboard } = useQuery({ queryKey: ['dashboard'], queryFn: queries.dashboard, refetchInterval: 10000 });
  const [editing, setEditing] = useState<NodeRecord | 'new'>();
  const [confirm, setConfirm] = useState<{ action: 'initialize' | 'reboot' | 'deinitialize' | 'delete' | 'removeLegacyKnownHosts'; node: NodeRecord }>();
  const [keyInstall, setKeyInstall] = useState<NodeRecord>();
  const [zoneEdit, setZoneEdit] = useState<NodeRecord>();
  const [zoneId, setZoneId] = useState('');
  const [zoneBusy, setZoneBusy] = useState(false);
  const [bootstrapPassword, setBootstrapPassword] = useState('');
  const [bootstrapBusy, setBootstrapBusy] = useState(false);
  const [revokeOnDelete, setRevokeOnDelete] = useState(false);
  const [error, setError] = useState('');
  const runtime = new Map((dashboard?.hosts || []).map((host) => [host.id, host]));
  const assignments = new Map<number, number>();
  clusters.forEach((cluster) => cluster.assignments.forEach((item) => assignments.set(item.node_id, (assignments.get(item.node_id) || 0) + 1)));

  const runAction = async () => {
    if (!confirm) return;
    setError('');
    try {
      if (confirm.action === 'removeLegacyKnownHosts') {
        await api(`/api/nodes/${confirm.node.id}/legacy-known-hosts/remove`, { method: 'POST' });
      }
      else if (confirm.action === 'delete') {
        const query = new URLSearchParams();
        if (revokeOnDelete) query.set('revoke_controller_key', 'true');
        // Deletion must never depend on a possibly stale host reachability or SSH-key state.
        // Key revocation is an explicit, separate operation selected in the dialog.
        else query.set('records_only', 'true');
        const result = await api<{ run_id?: number }>(`/api/nodes/${confirm.node.id}${query.size ? `?${query}` : ''}`, { method: 'DELETE' });
        if (result?.run_id) watchRun(result.run_id);
      }
      else {
        const result = await api<{ run_id: number }>(`/api/nodes/${confirm.node.id}/${confirm.action}`, { method: 'POST' });
        watchRun(result.run_id);
      }
      setConfirm(undefined); await queryClient.invalidateQueries();
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Host action failed'); setConfirm(undefined); }
  };
  const probe = async (node: NodeRecord) => {
    try { const result = await api<{ run_id: number }>(`/api/nodes/${node.id}/probe`, { method: 'POST' }); watchRun(result.run_id); }
    catch (reason) { setError((reason as Error).message); }
  };
  const installKey = async () => {
    if (!keyInstall || !bootstrapPassword) return;
    setBootstrapBusy(true); setError('');
    try {
      const result = await api<{ run_id: number }>(`/api/nodes/${keyInstall.id}/controller-key`, { method: 'POST', ...jsonBody({ password: bootstrapPassword }) });
      watchRun(result.run_id); setKeyInstall(undefined); setBootstrapPassword(''); await queryClient.invalidateQueries();
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Controller key installation failed'); }
    finally { setBootstrapBusy(false); }
  };
  const saveZone = async () => {
    if (!zoneEdit || !selectedCluster || !zoneId) return;
    setZoneBusy(true); setError('');
    try {
      const result = await api<{ run_id: number }>(`/api/nodes/${zoneEdit.id}/zone`, { method: 'PUT', ...jsonBody({ cluster_id: selectedCluster.id, zone_id: zoneId }) });
      watchRun(result.run_id); setZoneEdit(undefined); setZoneId(''); await queryClient.invalidateQueries();
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Unable to update host zone'); }
    finally { setZoneBusy(false); }
  };
  type HostItem = NodeRecord & { runtime?: HostRuntime; workload_count: number };
  const items: HostItem[] = nodes.map((node) => ({ ...node, runtime: runtime.get(node.id), workload_count: assignments.get(node.id) || 0 }));
  const columns: Array<EuiBasicTableColumn<HostItem>> = [
    { field: 'name', name: 'Host', render: (value: string, item: NodeRecord) => <div><strong>{value}</strong><small className="block-muted">{item.ssh_user}@{item.address}:{item.ssh_port}</small></div> },
    { field: 'runtime.os_name', name: 'Operating system', render: (value: string, item: HostItem) => <span>{item.runtime?.os_name || 'not observed'}</span> },
    { field: 'zone_id', name: 'Zone', render: (value?: string | null) => value ? <EuiBadge color="hollow">{value}</EuiBadge> : <span className="block-muted">not assigned</span> },
    { field: 'runtime.reachable', name: 'Reachability', render: (value: boolean, item: NodeRecord & { runtime?: HostRuntime }) => <EuiHealth color={value ? 'success' : 'danger'}>{value ? 'reachable' : item.runtime?.last_error || 'unreachable'}</EuiHealth> },
    { field: 'ssh_auth_state', name: 'SSH access', render: (value: string, item: NodeRecord) => <div><EuiBadge color={value === 'controller_key' ? 'success' : value === 'pending' || item.legacy_known_hosts_disabled ? 'warning' : 'hollow'}>{value === 'controller_key' ? 'controller key' : value === 'candidate_ready' ? 'candidate ready' : value === 'legacy' ? item.legacy_known_hosts_disabled ? 'legacy record removed' : 'legacy key' : 'pending'}</EuiBadge><small className="block-muted">{item.ssh_key_id || item.candidate_key_id || ''}</small></div> },
    { field: 'runtime.initialized', name: 'Initialized', render: (value: boolean) => <EuiBadge color={value ? 'success' : 'default'}>{value ? 'initialized' : 'uninitialized'}</EuiBadge> },
    { field: 'runtime.podman_socket_active', name: 'Podman socket', render: (value: boolean) => <EuiHealth color={value ? 'success' : 'subdued'}>{value ? 'active' : 'down'}</EuiHealth> },
    { field: 'runtime.podman_version', name: 'Podman version', render: (value: string, item: HostItem) => <span>{item.runtime?.podman_version || 'not installed or not observed'}</span> },
    { field: 'workload_count', name: 'Workloads' },
    { field: 'runtime.observed_at', name: 'Observed', render: (value?: string) => timeAgo(value) },
    { name: 'Actions', actions: [
      { name: 'Probe', description: 'Probe host over SSH', icon: 'inspect', type: 'icon' as const, onClick: probe },
      { name: 'Remove legacy known_hosts record', description: 'Stop using inherited legacy host-key trust for this host', icon: 'unlink', color: 'danger', type: 'icon' as const, available: (item: NodeRecord) => item.ssh_auth_state === 'legacy' && !item.legacy_known_hosts_disabled, onClick: (item: NodeRecord) => setConfirm({ action: 'removeLegacyKnownHosts', node: item }) },
      { name: 'Install controller key', description: 'Use a one-time password to install the controller SSH key', icon: 'key', type: 'icon' as const, onClick: (item: NodeRecord) => setKeyInstall(item) },
      { name: 'Edit zone', description: 'Assign a zone defined by the selected cluster', icon: 'globe', type: 'icon' as const, available: () => Boolean(selectedCluster?.zoning?.zones.length), onClick: (item: NodeRecord) => { setZoneId(item.zone_id || selectedCluster?.zoning?.zones[0] || ''); setZoneEdit(item); } },
      { name: 'Initialize', description: 'Install prerequisites and enable Podman socket', icon: 'play', type: 'icon' as const, onClick: (item: NodeRecord) => setConfirm({ action: 'initialize', node: item }) },
      { name: 'De-initialize', description: 'Remove controller-owned host setup', icon: 'stop', type: 'icon' as const, available: (item: HostItem) => item.runtime?.initialized === true, onClick: (item: NodeRecord) => setConfirm({ action: 'deinitialize', node: item }) },
      { name: 'Reboot', description: 'Restart this host and wait for it to return', icon: 'refresh', color: 'danger', type: 'icon' as const, onClick: (item: NodeRecord) => setConfirm({ action: 'reboot', node: item }) },
      { name: 'Edit', description: 'Edit inventory host', icon: 'pencil', type: 'icon' as const, onClick: (item: NodeRecord) => setEditing(item) },
      { name: 'Delete', description: 'Delete inventory record', icon: 'trash', color: 'danger', type: 'icon' as const, onClick: (item: NodeRecord) => { setRevokeOnDelete(false); setConfirm({ action: 'delete', node: item }); } },
    ] },
  ];
  return (
    <div className="page-stack">
      <div className="page-heading"><div><EuiTitle><h1>Host Config</h1></EuiTitle><EuiText color="subdued">SSH inventory, initialization state, and the controller-only Podman socket channel.</EuiText></div><EuiButton fill iconType="plusInCircle" onClick={() => setEditing('new')}>Add host</EuiButton></div>
      {error && <EuiCallOut title="Host operation failed" color="danger" iconType="warning">{error}</EuiCallOut>}
      {editing && <HostEditor node={editing === 'new' ? undefined : editing} cluster={selectedCluster} onClose={() => setEditing(undefined)} onRun={watchRun} />}
      <section className="section-band">
        <div className="section-heading"><div><EuiTitle size="s"><h2>Host inventory</h2></EuiTitle><EuiText color="subdued">The Podman API remains a rootful Unix socket and is forwarded through SSH only while monitored.</EuiText></div></div>
        <EuiBasicTable items={items} columns={columns} tableLayout="auto" />
        {!items.length && <div className="empty-host-state" role="status">
          <EuiIcon type="server" size="xxl" aria-label="Podman host server" />
          <div><EuiTitle size="xs"><h3>No hosts configured</h3></EuiTitle><EuiText size="s" color="subdued">Add the first Podman host to begin.</EuiText></div>
        </div>}
      </section>
      {confirm && <EuiConfirmModal title={`${confirm.action === 'removeLegacyKnownHosts' ? 'Remove legacy known_hosts record' : confirm.action === 'delete' ? 'Delete' : confirm.action === 'initialize' ? 'Initialize' : confirm.action === 'reboot' ? 'Reboot' : 'De-initialize'} ${confirm.node.name}`} onCancel={() => setConfirm(undefined)} onConfirm={runAction} cancelButtonText="Cancel" confirmButtonText={confirm.action === 'removeLegacyKnownHosts' ? 'Remove legacy record' : confirm.action === 'delete' ? revokeOnDelete ? 'Revoke key and delete' : 'Delete record only' : confirm.action === 'initialize' ? 'Initialize host' : confirm.action === 'reboot' ? 'Reboot host' : 'De-initialize host'} buttonColor={confirm.action === 'initialize' ? 'primary' : 'danger'} defaultFocusedButton="cancel">
        {confirm.action === 'initialize' && 'Sets SELinux permissive immediately and disabled for the next maintenance reboot, without rebooting this host. It then installs prerequisites, records controller ownership, and enables the rootful Podman Unix socket.'}
        {confirm.action === 'reboot' && 'Restarts the operating system. All workloads on this host will stop until it returns; the controller waits for SSH connectivity before completing the run.'}
        {confirm.action === 'deinitialize' && 'Blocked while managed workloads remain. Packages, images, and unrelated Podman resources are preserved.'}
        {confirm.action === 'removeLegacyKnownHosts' && 'Stops ELKeeper from using the inherited legacy known_hosts record for this host. The mounted legacy known_hosts file remains read-only and unchanged.'}
        {confirm.action === 'delete' && 'This removes the controller inventory record and is blocked while cluster memberships remain.'}
        {confirm.action === 'delete' && ['controller_key', 'candidate_ready'].includes(confirm.node.ssh_auth_state || '') && <><EuiSpacer size="s" /><EuiSwitch id="revoke-controller-key" label="Remove the installed controller key before deleting this host" checked={revokeOnDelete} onChange={(event) => setRevokeOnDelete(event.target.checked)} /><EuiText size="s" color="subdued"><p>{revokeOnDelete ? 'Yes: the controller key is revoked before the inventory record is deleted.' : 'No: this is a records-only deletion and the controller key remains on the host.'}</p></EuiText></>}
        {confirm.action === 'delete' && !revokeOnDelete && <><EuiSpacer size="s" /><EuiCallOut size="s" color="warning" title="Records-only deletion"><p>Controller key will remain on the host. Use this only when the host is unreachable or you intentionally want to keep that key.</p></EuiCallOut></>}
      </EuiConfirmModal>}
      {keyInstall && <EuiOverlayMask><EuiModal onClose={() => { setKeyInstall(undefined); setBootstrapPassword(''); }} initialFocus="[data-autofocus]">
        <EuiModalHeader><EuiModalHeaderTitle>Install controller key on {keyInstall.name}</EuiModalHeaderTitle></EuiModalHeader>
        <EuiModalBody><EuiText size="s"><p>The password is used once to add the current controller public key to <strong>{keyInstall.ssh_user}</strong>. It is not retained.</p></EuiText><EuiSpacer /><EuiFormRow label="Host password"><EuiFieldPassword data-autofocus value={bootstrapPassword} onChange={(event) => setBootstrapPassword(event.target.value)} autoComplete="new-password" onKeyDown={(event) => { if (event.key === 'Enter') installKey(); }} /></EuiFormRow></EuiModalBody>
        <EuiModalFooter><EuiButtonEmpty onClick={() => { setKeyInstall(undefined); setBootstrapPassword(''); }}>Cancel</EuiButtonEmpty><EuiButton fill onClick={installKey} isLoading={bootstrapBusy} disabled={!bootstrapPassword}>Install key</EuiButton></EuiModalFooter>
      </EuiModal></EuiOverlayMask>}
      {zoneEdit && selectedCluster && <EuiOverlayMask><EuiModal onClose={() => { setZoneEdit(undefined); setZoneId(''); }} initialFocus="[data-autofocus]">
        <EuiModalHeader><EuiModalHeaderTitle>Edit zone for {zoneEdit.name}</EuiModalHeaderTitle></EuiModalHeader>
        <EuiModalBody><EuiText size="s"><p>The host zone is a physical attribute shared by every cluster that uses this host. Active Elasticsearch workloads are reconciled and rolled back if readiness fails.</p></EuiText><EuiSpacer /><EuiFormRow label="Host zone"><EuiSelect data-autofocus value={zoneId} onChange={(event) => setZoneId(event.target.value)} options={(selectedCluster.zoning?.zones || []).map((zone) => ({ value: zone, text: zone }))} /></EuiFormRow></EuiModalBody>
        <EuiModalFooter><EuiButtonEmpty onClick={() => { setZoneEdit(undefined); setZoneId(''); }}>Cancel</EuiButtonEmpty><EuiButton fill onClick={saveZone} isLoading={zoneBusy} disabled={!zoneId}>Save zone</EuiButton></EuiModalFooter>
      </EuiModal></EuiOverlayMask>}
    </div>
  );
}
