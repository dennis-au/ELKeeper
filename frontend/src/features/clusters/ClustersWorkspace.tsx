import { useEffect, useState } from 'react';
import {
  EuiBadge, EuiButton, EuiButtonEmpty, EuiCallOut, EuiColorPicker, EuiConfirmModal, EuiFieldNumber,
  EuiFieldText, EuiFlexGroup, EuiFlexItem, EuiForm, EuiFormRow, EuiIconTip, EuiPanel, EuiSelect, EuiSpacer,
  EuiSwitch, EuiText, EuiTitle,
} from '@elastic/eui';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { clusterApi } from './index';
import { useConsole } from '../../app-context';
import { MaintenancePolicyEditor } from '../maintenance/components';
import type { Cluster, ElasticsearchSettings, LogMonitoring, PortProfile, RolePortProfile, ZoningConfig } from './types';
import type { VersionResponse } from '../versions';

const defaultPorts: PortProfile = { elasticsearch_http: 9200, elasticsearch_transport: 9300, kibana: 5601, fleet: 8220, logstash_api: 9600 };
const elasticsearchRoleOrder = ['master', 'hot', 'warm', 'ml', 'ingest', 'coordinating'] as const;
const rolePortGroups = [
  { id: 'master', label: 'Master', ports: [{ id: 'elasticsearch_http', label: 'HTTP' }, { id: 'elasticsearch_transport', label: 'Transport' }] },
  { id: 'hot', label: 'Hot data', ports: [{ id: 'elasticsearch_http', label: 'HTTP' }, { id: 'elasticsearch_transport', label: 'Transport' }] },
  { id: 'warm', label: 'Warm data', ports: [{ id: 'elasticsearch_http', label: 'HTTP' }, { id: 'elasticsearch_transport', label: 'Transport' }] },
  { id: 'ml', label: 'Machine learning', ports: [{ id: 'elasticsearch_http', label: 'HTTP' }, { id: 'elasticsearch_transport', label: 'Transport' }] },
  { id: 'ingest', label: 'Ingest', ports: [{ id: 'elasticsearch_http', label: 'HTTP' }, { id: 'elasticsearch_transport', label: 'Transport' }] },
  { id: 'coordinating', label: 'Coordinating', ports: [{ id: 'elasticsearch_http', label: 'HTTP' }, { id: 'elasticsearch_transport', label: 'Transport' }] },
  { id: 'kibana', label: 'Kibana', ports: [{ id: 'kibana', label: 'Listener' }] },
  { id: 'fleet-server', label: 'Fleet Server', ports: [{ id: 'fleet', label: 'Listener' }] },
  { id: 'logstash', label: 'Logstash', ports: [{ id: 'logstash_api', label: 'API' }] },
] as const;
const elasticsearchPortGroups = rolePortGroups.filter((group) => group.ports.length === 2);
const servicePortGroups = rolePortGroups.filter((group) => group.ports.length === 1);
const defaultSettings: ElasticsearchSettings = {
  allocation_enable: 'all', rebalance_enable: 'all', disk_watermark_low: '85%', disk_watermark_high: '90%',
  disk_watermark_flood_stage: '95%', recovery_max_bytes_per_sec: '40mb',
};
const disabledZoning: ZoningConfig = { mode: 'disabled', zones: [] };

function defaultRolePorts(legacyPorts: PortProfile = defaultPorts): RolePortProfile {
  return {
    ...Object.fromEntries(elasticsearchRoleOrder.map((role, offset) => [role, {
      elasticsearch_http: legacyPorts.elasticsearch_http + offset,
      elasticsearch_transport: legacyPorts.elasticsearch_transport + offset,
    }])),
    kibana: { kibana: legacyPorts.kibana },
    'fleet-server': { fleet: legacyPorts.fleet },
    logstash: { logstash_api: legacyPorts.logstash_api },
    'elastic-agent': {},
  };
}

function rolePortsFor(cluster?: Cluster): RolePortProfile {
  return cluster?.role_ports || defaultRolePorts(cluster?.ports || defaultPorts);
}

function legacyPortsFromRolePorts(rolePorts: RolePortProfile): PortProfile {
  return {
    elasticsearch_http: rolePorts.master.elasticsearch_http,
    elasticsearch_transport: rolePorts.master.elasticsearch_transport,
    kibana: rolePorts.kibana.kibana,
    fleet: rolePorts['fleet-server'].fleet,
    logstash_api: rolePorts.logstash.logstash_api,
  };
}

function validRolePorts(rolePorts: RolePortProfile) {
  const values = rolePortGroups.flatMap((group) => group.ports.map((port) => rolePorts[group.id]?.[port.id]));
  return values.every((value) => value !== undefined && Number.isInteger(value) && value >= 1 && value <= 65535) && new Set(values).size === values.length;
}

function ClusterEditor({ cluster, onClose }: { cluster?: Cluster; onClose: () => void }) {
  const queryClient = useQueryClient();
  const { setSelectedClusterId } = useConsole();
  const [name, setName] = useState(cluster?.name || '');
  const [themeColor, setThemeColor] = useState(cluster?.theme_color || '#0077CC');
  const [networkMode, setNetworkMode] = useState(cluster?.network_defaults.mode || 'shared');
  const [zoningMode, setZoningMode] = useState<ZoningConfig['mode']>(cluster?.zoning?.mode || 'disabled');
  const [zones, setZones] = useState<string[]>(cluster?.zoning?.zones || []);
  const [rolePorts, setRolePorts] = useState<RolePortProfile>(() => rolePortsFor(cluster));
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const updateRolePort = (role: string, port: string, value: number) => setRolePorts((current) => ({ ...current, [role]: { ...current[role], [port]: value } }));
  const save = async (event: React.FormEvent) => {
    event.preventDefault(); setBusy(true); setError('');
    if (!validRolePorts(rolePorts)) {
      setError('Every role port must be an integer from 1 through 65535 and unique across all roles.');
      setBusy(false);
      return;
    }
    const normalizedZones = zones.map((zone) => zone.trim().toLowerCase()).filter(Boolean);
    if (zoningMode !== 'disabled' && normalizedZones.length < 2) {
      setError('Zone awareness requires at least two zones.');
      setBusy(false);
      return;
    }
    if (new Set(normalizedZones).size !== normalizedZones.length) {
      setError('Zone IDs must be unique.');
      setBusy(false);
      return;
    }
    try {
      const result = await clusterApi.save(cluster?.id, { name, theme_color: themeColor, ...(cluster ? { desired_version: cluster.desired_version } : {}), ports: legacyPortsFromRolePorts(rolePorts), role_ports: rolePorts, network_defaults: { mode: networkMode }, zoning: { mode: zoningMode, zones: normalizedZones }, elasticsearch_settings: cluster?.elasticsearch_settings || defaultSettings });
      await queryClient.invalidateQueries({ queryKey: ['clusters'] });
      if (result?.id) setSelectedClusterId(result.id);
      onClose();
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Unable to save cluster'); }
    finally { setBusy(false); }
  };
  return (
    <EuiPanel hasBorder paddingSize="l">
      <EuiForm component="form" onSubmit={save}>
        <EuiTitle size="s"><h2>{cluster ? 'Edit cluster' : 'Create cluster'}</h2></EuiTitle><EuiSpacer />
        {error && <><EuiCallOut title="Cluster configuration failed" color="danger" iconType="warning">{error}</EuiCallOut><EuiSpacer size="m" /></>}
        <div className="form-grid">
          <EuiFormRow label="Cluster name"><EuiFieldText value={name} onChange={(event) => setName(event.target.value)} required /></EuiFormRow>
          <EuiFormRow label="Theme color"><EuiColorPicker color={themeColor} onChange={setThemeColor} /></EuiFormRow>
          <EuiFormRow label="Default network mode"><EuiSelect value={networkMode} onChange={(event) => setNetworkMode(event.target.value as 'shared' | 'dedicated')} options={[{ value: 'shared', text: 'Shared system and user NIC' }, { value: 'dedicated', text: 'Dedicated system and user NICs' }]} /></EuiFormRow>
          <section className="role-port-associations cluster-zoning-config" aria-labelledby="availability-zones-heading">
            <div className="cluster-zoning-config__heading">
              <div className="role-port-associations__heading"><EuiTitle size="xxs"><h3 id="availability-zones-heading">Availability zones</h3></EuiTitle><EuiText size="s" color="subdued">Define the physical failure domains that hosts may join. Runtime allocation changes only when zoning is applied.</EuiText></div>
              <EuiButtonEmpty type="button" size="s" iconType="plusInCircle" onClick={() => setZones((current) => [...current, ''])}>Add zone</EuiButtonEmpty>
            </div>
            <div className="cluster-zoning-config__layout">
              <div className="cluster-zoning-config__mode">
                <EuiFormRow label="Zone awareness mode" fullWidth><EuiSelect fullWidth value={zoningMode} onChange={(event) => setZoningMode(event.target.value as ZoningConfig['mode'])} options={[{ value: 'disabled', text: 'Disabled' }, { value: 'awareness', text: 'Allocation awareness' }, { value: 'forced_awareness', text: 'Forced awareness' }]} /></EuiFormRow>
              </div>
              <div className="cluster-zone-catalog" aria-label="Defined zones">
                <div className="cluster-zone-catalog__heading"><EuiTitle size="xxs"><h4>Defined zones</h4></EuiTitle><EuiBadge color="hollow">{zones.length}</EuiBadge></div>
                {zones.length ? <div className="cluster-zone-catalog__grid">
                  {zones.map((zone, index) => <div className="zone-catalog-row" key={index}><EuiFormRow fullWidth label={`Zone ${index + 1}`}><EuiFieldText fullWidth value={zone} onChange={(event) => setZones((current) => current.map((item, itemIndex) => itemIndex === index ? event.target.value : item))} placeholder="zone-a" /></EuiFormRow><EuiButtonEmpty type="button" iconType="trash" color="danger" aria-label={`Remove zone ${index + 1}`} onClick={() => setZones((current) => current.filter((_, itemIndex) => itemIndex !== index))} /></div>)}
                </div> : <div className="cluster-zone-catalog__empty"><EuiText size="s" color="subdued">No zones defined.</EuiText></div>}
              </div>
            </div>
          </section>
          <section className="role-port-associations" aria-labelledby="role-port-associations-heading">
            <div className="role-port-associations__heading"><EuiTitle size="xxs"><h3 id="role-port-associations-heading">Role port associations</h3></EuiTitle><EuiText size="s" color="subdued">Suggested role-specific listeners prevent collisions when multiple workloads share a host.</EuiText></div>
            <div className="role-port-section">
              <div className="role-port-section__heading"><EuiTitle size="xxs"><h4 id="elasticsearch-node-ports-heading">Elasticsearch node ports</h4></EuiTitle><EuiText size="s" color="subdued">Transport ports are used for Elasticsearch node-to-node communication.</EuiText></div>
              <div className="role-port-grid" aria-labelledby="elasticsearch-node-ports-heading">
                <div className="role-port-grid__header">Role</div><div className="role-port-grid__header">HTTP port</div><div className="role-port-grid__header">Transport port</div>
                {elasticsearchPortGroups.flatMap((group) => [
                  <div className="role-port-grid__role" key={`${group.id}-role`}><strong>{group.label}</strong></div>,
                  ...group.ports.map((port, index) => <div className={`role-port-field${index ? ' role-port-field--secondary' : ''}`} key={`${group.id}-${port.id}`}><EuiFormRow fullWidth label={`${port.label} port`}><EuiFieldNumber fullWidth aria-label={`${group.label} ${port.label} port`} min={1} max={65535} value={rolePorts[group.id]?.[port.id] ?? ''} onChange={(event) => updateRolePort(group.id, port.id, Number(event.target.value))} /></EuiFormRow></div>),
                ])}
              </div>
            </div>
            <div className="role-port-section">
              <div className="role-port-section__heading"><EuiTitle size="xxs"><h4 id="service-listeners-heading">Service listeners</h4></EuiTitle></div>
              <div className="role-port-service-grid" aria-labelledby="service-listeners-heading">
                {servicePortGroups.flatMap((group) => group.ports.map((port) => <div className="role-port-service-field" key={`${group.id}-${port.id}`}><EuiFormRow fullWidth label={`${group.label} ${port.label} port`}><EuiFieldNumber fullWidth min={1} max={65535} value={rolePorts[group.id]?.[port.id] ?? ''} onChange={(event) => updateRolePort(group.id, port.id, Number(event.target.value))} /></EuiFormRow></div>))}
              </div>
            </div>
          </section>
        </div>
        <EuiSpacer />
        <EuiFlexGroup justifyContent="flexEnd" gutterSize="s"><EuiButtonEmpty onClick={onClose}>Cancel</EuiButtonEmpty><EuiButton type="submit" fill isLoading={busy}>Save cluster</EuiButton></EuiFlexGroup>
      </EuiForm>
      {cluster && <div className="cluster-editor-operations">
        <MaintenancePolicyEditor clusterId={cluster.id} />
        <ZoningPanel cluster={cluster} />
        <SettingsEditor cluster={cluster} />
        <LogMonitoringPanel cluster={cluster} />
        <VersionsPanel cluster={cluster} />
      </div>}
    </EuiPanel>
  );
}

function ZoningPanel({ cluster }: { cluster: Cluster }) {
  const { watchRun, refreshAll } = useConsole();
  const [message, setMessage] = useState('');
  const mutation = useMutation({
    mutationFn: () => clusterApi.applyZoning(cluster.id),
    onSuccess: async (result) => { setMessage('Zoning reconciliation started.'); watchRun(result.run_id); await refreshAll(); },
    onError: (error) => setMessage((error as Error).message),
  });
  const zoning = cluster.zoning || disabledZoning;
  const status = cluster.zoning_status?.status || (zoning.mode === 'disabled' ? 'disabled' : 'pending');
  const statusColor = status === 'applied' ? 'success' : status === 'failed' || status === 'drift' ? 'danger' : status === 'pending' ? 'warning' : 'hollow';
  return (
    <section className="section-band">
      <div className="section-heading"><div><EuiTitle size="s"><h2>Availability zoning</h2></EuiTitle><EuiText color="subdued">Applies <code>node.attr.zone</code> and the matching cluster allocation-awareness settings through a guarded run.</EuiText></div><EuiBadge color={statusColor}>{status.replace('_', ' ')}</EuiBadge></div>
      <div className="config-summary">
        <span><strong>Desired mode</strong>{zoning.mode.replace('_', ' ')}</span>
        <span><strong>Defined zones</strong>{zoning.zones.join(', ') || 'none'}</span>
        <span><strong>Applied mode</strong>{cluster.zoning_status?.applied_mode?.replace('_', ' ') || 'not observed'}</span>
        <span><strong>Last observation</strong>{cluster.zoning_status?.observed_at || 'not observed'}</span>
      </div>
      {cluster.zoning_status?.last_error && <><EuiSpacer size="s" /><EuiCallOut size="s" color="danger" title="Zoning needs attention">{cluster.zoning_status.last_error}</EuiCallOut></>}
      <EuiSpacer size="m" /><EuiFlexGroup gutterSize="s" alignItems="center" responsive={false}><EuiFlexItem grow={false}><EuiButton fill onClick={() => mutation.mutate()} isLoading={mutation.isPending}>Apply zoning</EuiButton></EuiFlexItem><EuiFlexItem grow={false}><EuiIconTip type="question" content="Data roles are reconciled before master-eligible nodes. Forced awareness is blocked until every active data tier covers every configured zone." /></EuiFlexItem></EuiFlexGroup>
      {message && <><EuiSpacer size="s" /><EuiCallOut size="s" title={message} color={mutation.isError ? 'danger' : 'success'} /></>}
    </section>
  );
}

function SettingsEditor({ cluster }: { cluster: Cluster }) {
  const { watchRun, refreshAll } = useConsole();
  const [settings, setSettings] = useState(cluster.elasticsearch_settings || defaultSettings);
  const [message, setMessage] = useState('');
  useEffect(() => setSettings(cluster.elasticsearch_settings || defaultSettings), [cluster]);
  const mutation = useMutation({
    mutationFn: () => clusterApi.updateSettings(cluster.id, settings),
    onSuccess: async (result) => { setMessage('Settings saved and queued for verification.'); watchRun(result.run_id); await refreshAll(); },
    onError: (error) => setMessage((error as Error).message),
  });
  const field = (key: keyof ElasticsearchSettings, label: string) => <EuiFormRow label={label}><EuiFieldText value={settings[key]} onChange={(event) => setSettings((current) => ({ ...current, [key]: event.target.value }))} /></EuiFormRow>;
  return (
    <section className="section-band">
      <div className="section-heading"><div><EuiTitle size="s"><h2>Elasticsearch settings</h2></EuiTitle><EuiText color="subdued">Validated persistent settings; the raw cluster response remains read-only on the dashboard.</EuiText></div></div>
      <div className="form-grid">
        <EuiFormRow label="Allocation"><EuiSelect value={settings.allocation_enable} onChange={(event) => setSettings({ ...settings, allocation_enable: event.target.value as ElasticsearchSettings['allocation_enable'] })} options={['all', 'primaries', 'new_primaries', 'none'].map((value) => ({ value, text: value }))} /></EuiFormRow>
        <EuiFormRow label="Rebalance"><EuiSelect value={settings.rebalance_enable} onChange={(event) => setSettings({ ...settings, rebalance_enable: event.target.value as ElasticsearchSettings['rebalance_enable'] })} options={['all', 'primaries', 'replicas', 'none'].map((value) => ({ value, text: value }))} /></EuiFormRow>
        {field('disk_watermark_low', 'Disk watermark low')}{field('disk_watermark_high', 'Disk watermark high')}{field('disk_watermark_flood_stage', 'Flood-stage watermark')}{field('recovery_max_bytes_per_sec', 'Recovery throughput')}
      </div>
      <EuiSpacer /><EuiButton fill onClick={() => mutation.mutate()} isLoading={mutation.isPending}>Apply settings</EuiButton>
      {message && <><EuiSpacer size="s" /><EuiCallOut size="s" title={message} color={mutation.isError ? 'danger' : 'success'} /></>}
    </section>
  );
}

function companionState(cluster: Cluster): NonNullable<LogMonitoring['companion_state']> {
  if (!cluster.log_monitoring?.filebeat_enabled) return 'disabled';
  const states = cluster.assignments.map((assignment) => assignment.filebeat?.state || 'pending');
  if (states.some((state) => state === 'degraded')) return 'degraded';
  if (states.length > 0 && states.every((state) => state === 'running')) return 'running';
  return 'pending';
}

function LogMonitoringPanel({ cluster }: { cluster: Cluster }) {
  const { watchRun, refreshAll } = useConsole();
  const [enabled, setEnabled] = useState(cluster.log_monitoring?.filebeat_enabled ?? false);
  const [message, setMessage] = useState('');
  useEffect(() => setEnabled(cluster.log_monitoring?.filebeat_enabled ?? false), [cluster]);
  const mutation = useMutation({
    mutationFn: () => clusterApi.updateLogMonitoring(cluster.id, { filebeat_enabled: enabled }),
    onSuccess: async (result) => { setMessage(enabled ? 'Log monitoring reconciliation started.' : 'Filebeat companions are being removed.'); watchRun(result.run_id); await refreshAll(); },
    onError: (error) => setMessage((error as Error).message),
  });
  const status = companionState(cluster);
  const color = status === 'running' ? 'success' : status === 'degraded' ? 'danger' : status === 'pending' ? 'warning' : 'hollow';
  return (
    <section className="section-band">
      <div className="section-heading"><div><EuiTitle size="s"><h2>Log monitoring</h2></EuiTitle><EuiText color="subdued">Managed workload container logs are sent to Kibana through Filebeat companions.</EuiText></div><EuiBadge color={color}>{status}</EuiBadge></div>
      <EuiFlexGroup alignItems="center" gutterSize="m" responsive={false} wrap>
        <EuiFlexItem grow={false}><EuiSwitch label="Enable Filebeat companions" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} /></EuiFlexItem>
        <EuiFlexItem grow={false}><EuiText size="s" color="subdued">Retention: {cluster.log_monitoring?.retention_days ?? 30} days</EuiText></EuiFlexItem>
        <EuiFlexItem grow={false}><EuiButton fill onClick={() => mutation.mutate()} isLoading={mutation.isPending}>Apply log monitoring</EuiButton></EuiFlexItem>
      </EuiFlexGroup>
      {message && <><EuiSpacer size="s" /><EuiCallOut size="s" title={message} color={mutation.isError ? 'danger' : 'success'} /></>}
    </section>
  );
}

function VersionsPanel({ cluster }: { cluster: Cluster }) {
  const { watchRun } = useConsole();
  const [target, setTarget] = useState('');
  const [confirm, setConfirm] = useState<'download' | 'upgrade'>();
  const { data, isLoading } = useQuery({ queryKey: ['versions', cluster.id], queryFn: () => clusterApi.versions(cluster.id) });
  useEffect(() => { if (!target && data?.available_versions[0]) setTarget(data.available_versions[0]); }, [data, target]);
  const run = async (kind: 'download' | 'upgrade') => {
    const result = await clusterApi.versionAction(cluster.id, kind, target);
    watchRun(result.run_id); setConfirm(undefined);
  };
  return (
    <section className="section-band">
      <div className="section-heading"><div><EuiTitle size="s"><h2>Versions</h2></EuiTitle><EuiText color="subdued">Download is non-disruptive. Upgrade uses existing health and master-redundancy gates.</EuiText></div></div>
      <EuiFlexGroup alignItems="flexEnd" gutterSize="m">
        <EuiFlexItem><EuiFormRow label="Available stable version"><EuiSelect isLoading={isLoading} value={target} onChange={(event) => setTarget(event.target.value)} options={(data?.available_versions || []).map((version) => ({ value: version, text: version }))} /></EuiFormRow></EuiFlexItem>
        <EuiFlexItem grow={false}><EuiButton onClick={() => setConfirm('download')} disabled={!target}>Download only</EuiButton></EuiFlexItem>
        <EuiFlexItem grow={false}><EuiButton fill color="warning" onClick={() => setConfirm('upgrade')} disabled={!target}>Upgrade cluster</EuiButton></EuiFlexItem>
      </EuiFlexGroup>
      {data?.registry_error && <EuiCallOut title="Registry unavailable" color="warning">{data.registry_error}</EuiCallOut>}
      <div className="version-list">{data?.assignments.map((item) => <div key={item.id}><strong>{item.role} on {item.node_name}</strong><span>{item.observation?.version || 'not observed'} → {item.desired_version}</span><EuiBadge color={item.observation?.cached ? 'success' : 'default'}>{item.observation?.cached ? 'cached' : 'not cached'}</EuiBadge></div>)}</div>
      {confirm && <EuiConfirmModal title={confirm === 'download' ? `Download ${target}` : `Upgrade to ${target}`} onCancel={() => setConfirm(undefined)} onConfirm={() => run(confirm)} cancelButtonText="Cancel" confirmButtonText={confirm === 'download' ? 'Download images' : 'Start guarded upgrade'} buttonColor={confirm === 'upgrade' ? 'danger' : 'primary'} defaultFocusedButton="cancel">
        {confirm === 'download' ? 'Images are pulled without changing or restarting workloads.' : 'The controller will stop at the first failed health or safety gate.'}
      </EuiConfirmModal>}
    </section>
  );
}

export function ClustersWorkspace() {
  const queryClient = useQueryClient();
  const { clusters, selectedCluster, selectedClusterId, setSelectedClusterId } = useConsole();
  const [editor, setEditor] = useState<'new' | 'edit'>();
  const [deleting, setDeleting] = useState(false);
  const selectedRolePorts = rolePortsFor(selectedCluster);
  const remove = async () => {
    if (!selectedCluster) return;
    await clusterApi.remove(selectedCluster.id);
    setDeleting(false); setSelectedClusterId(0); await queryClient.invalidateQueries({ queryKey: ['clusters'] });
  };
  return (
    <div className="page-stack">
      <div className="page-heading"><div><EuiTitle><h1>Cluster Config</h1></EuiTitle><EuiText color="subdued">Cluster identity, network profile, role ports, and safe Elasticsearch settings.</EuiText></div><EuiButton fill iconType="plusInCircle" onClick={() => setEditor('new')}>Create cluster</EuiButton></div>
      {editor && <ClusterEditor cluster={editor === 'edit' ? selectedCluster : undefined} onClose={() => setEditor(undefined)} />}
      <section className="cluster-config-layout">
        <EuiPanel hasBorder paddingSize="s" className="cluster-inventory">
          {clusters.map((cluster) => <button key={cluster.id} className={cluster.id === selectedClusterId ? 'is-selected' : ''} onClick={() => setSelectedClusterId(cluster.id)}><span className="cluster-dot" style={{ background: cluster.theme_color }} /><span><strong>{cluster.name}</strong><small>{cluster.assignments.length} workloads</small></span></button>)}
          {!clusters.length && <EuiText color="subdued">No clusters configured.</EuiText>}
        </EuiPanel>
        <div className="page-stack">
          {selectedCluster ? <>
            <section className="section-band">
              <div className="section-heading"><div><EuiTitle size="s"><h2>{selectedCluster.name}</h2></EuiTitle><EuiText color="subdued">{selectedCluster.slug}</EuiText></div><EuiFlexGroup gutterSize="s"><EuiButtonEmpty iconType="pencil" onClick={() => setEditor('edit')}>Edit</EuiButtonEmpty><EuiButtonEmpty color="danger" iconType="trash" onClick={() => setDeleting(true)}>Delete</EuiButtonEmpty></EuiFlexGroup></div>
              <div className="config-summary"><span><strong>Theme</strong><i style={{ background: selectedCluster.theme_color }} />{selectedCluster.theme_color}</span><span><strong>Network default</strong>{selectedCluster.network_defaults.mode}</span><span><strong>Zoning</strong>{(selectedCluster.zoning || disabledZoning).mode.replace('_', ' ')}{(selectedCluster.zoning || disabledZoning).zones.length ? ` · ${(selectedCluster.zoning || disabledZoning).zones.join(', ')}` : ''}</span><span><strong>Master ports</strong>HTTP {selectedRolePorts.master.elasticsearch_http} · Transport {selectedRolePorts.master.elasticsearch_transport}</span><span><strong>Hot data ports</strong>HTTP {selectedRolePorts.hot.elasticsearch_http} · Transport {selectedRolePorts.hot.elasticsearch_transport}</span></div>
            </section>
          </> : <EuiCallOut title="Select or create a cluster" iconType="cluster" />}
        </div>
      </section>
      {deleting && selectedCluster && <EuiConfirmModal title={`Delete ${selectedCluster.name}`} onCancel={() => setDeleting(false)} onConfirm={remove} cancelButtonText="Cancel" confirmButtonText="Delete empty cluster" buttonColor="danger" defaultFocusedButton="cancel">All workloads and memberships must already be removed.</EuiConfirmModal>}
    </div>
  );
}
