import { useEffect, useState, type ReactNode } from 'react';
import {
  EuiBadge, EuiBasicTable, EuiButton, EuiButtonEmpty, EuiCallOut, EuiFlexGroup, EuiFlexItem, EuiHealth, EuiPanel,
  EuiLink, EuiProgress, EuiSpacer, EuiStat, EuiText, EuiTitle, EuiToolTip,
} from '@elastic/eui';
import ReactECharts from 'echarts-for-react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { dashboardApi } from './index';
import { useConsole } from '../../app-context';
import { bytes, formatDateTime, formatTime, percent, timeAgo } from '../../shared/format';
import type { ClusterMetrics, ClusterSummary, ContainerMetric, ControllerSettings, CrossClusterHostUsage, DashboardSnapshot, Health, HostResourceSample, LogMonitoring, NodeBreakdown, TopologyResponse, ZoneBreakdown } from './types';

const healthColor: Record<Health, 'success' | 'warning' | 'danger' | 'subdued'> = {
  green: 'success', yellow: 'warning', red: 'danger', unknown: 'subdued', awaiting_data: 'subdued',
};

const healthDescription: Record<Health, string> = {
  green: 'All primary and replica shards are assigned and configured hosts are reachable.',
  yellow: 'Primary shards are assigned, but replica allocation or host availability needs attention.',
  red: 'One or more primary shards are unassigned and data may be unavailable.',
  unknown: 'Elasticsearch health cannot be determined right now.',
  awaiting_data: 'A master is running, but a Hot data or Warm data role is required before Elasticsearch can allocate data and report health.',
};

function healthLabel(health: Health) {
  return health === 'awaiting_data' ? 'Awaiting data role' : `${health[0].toUpperCase()}${health.slice(1)}`;
}

function HealthStatus({ health, children }: { health: Health; children: ReactNode }) {
  const description = healthDescription[health];
  const label = `${healthLabel(health)} status: ${description}`;
  return <EuiToolTip content={description}><div tabIndex={0} aria-label={label}><EuiHealth color={healthColor[health]}>{children}</EuiHealth></div></EuiToolTip>;
}

function monitoringState(monitoring?: LogMonitoring): NonNullable<LogMonitoring['companion_state']> {
  if (!monitoring?.filebeat_enabled) return 'disabled';
  return monitoring.companion_state || 'pending';
}

function monitoringColor(state: NonNullable<LogMonitoring['companion_state']>): 'success' | 'warning' | 'danger' | 'hollow' {
  return state === 'running' ? 'success' : state === 'degraded' ? 'danger' : state === 'pending' ? 'warning' : 'hollow';
}

function kibanaLogsUrl(endpoint: string, slug: string) {
  const dataset = `elkeeper.${slug}`;
  const dataViewId = `elkeeper-logs-${slug}`;
  return `${endpoint}/app/discover#/?_a=(columns:!(),dataSource:(dataViewId:'${dataViewId}',type:dataView),filters:!((meta:(alias:!n,disabled:!f,index:'${dataViewId}',key:data_stream.dataset,negate:!f,params:(query:'${dataset}'),type:phrase),query:(match_phrase:(data_stream.dataset:'${dataset}')))),interval:auto,query:(language:kuery,query:''),sort:!())`;
}

function Metric({ title, value, description }: { title: string; value: string | number; description?: string }) {
  return <EuiPanel paddingSize="m" hasBorder><EuiStat title={String(value)} description={title} titleSize="s"><EuiText size="xs" color="subdued">{description}</EuiText></EuiStat></EuiPanel>;
}

function rate(value: number | null | undefined) {
  return typeof value === 'number' && Number.isFinite(value) ? `${bytes(value)}/s` : 'Awaiting sample';
}

function usagePercent(used: number, total: number) {
  return total > 0 ? percent(used, total) : undefined;
}

function hostChartAxis(history: HostResourceSample[], timezone: string) {
  return { type: 'category', boundaryGap: false, data: history.map((sample) => formatTime(sample.observed_at, timezone)), axisLabel: { hideOverlap: true } };
}

function HostResourceUsage({ host, timezone }: { host: CrossClusterHostUsage; timezone: string }) {
  const history = host.history || [];
  const latest = history[history.length - 1];
  const memory = latest ? usagePercent(latest.memory_usage_bytes, latest.memory_total_bytes) : undefined;
  const rateAxis = { type: 'value', axisLabel: { formatter: (value: number) => `${bytes(value)}/s` } };
  const resourceChart = {
    animation: false,
    grid: { left: 42, right: 10, top: 30, bottom: 26 },
    tooltip: { trigger: 'axis', valueFormatter: (value: number) => `${value}%` },
    legend: { top: 0, data: ['CPU', 'Memory'] },
    xAxis: hostChartAxis(history, timezone),
    yAxis: { type: 'value', min: 0, max: 100, axisLabel: { formatter: '{value}%' } },
    series: [
      { name: 'CPU', type: 'line', showSymbol: false, data: history.map((sample) => sample.cpu_percent), itemStyle: { color: '#0077CC' } },
      { name: 'Memory', type: 'line', showSymbol: false, data: history.map((sample) => usagePercent(sample.memory_usage_bytes, sample.memory_total_bytes)), itemStyle: { color: '#00A67E' } },
    ],
  };
  const networkChart = {
    animation: false,
    grid: { left: 56, right: 10, top: 30, bottom: 26 },
    tooltip: { trigger: 'axis', valueFormatter: (value: number) => `${bytes(value)}/s` },
    legend: { top: 0, data: ['Received', 'Sent'] },
    xAxis: hostChartAxis(history, timezone),
    yAxis: rateAxis,
    series: [
      { name: 'Received', type: 'line', showSymbol: false, data: history.map((sample) => sample.network_rx_bytes_per_second), itemStyle: { color: '#0077CC' } },
      { name: 'Sent', type: 'line', showSymbol: false, data: history.map((sample) => sample.network_tx_bytes_per_second), itemStyle: { color: '#E7664C' } },
    ],
  };
  const diskChart = {
    animation: false,
    grid: { left: 56, right: 10, top: 30, bottom: 26 },
    tooltip: { trigger: 'axis', valueFormatter: (value: number) => `${bytes(value)}/s` },
    legend: { top: 0, data: ['Read', 'Write'] },
    xAxis: hostChartAxis(history, timezone),
    yAxis: rateAxis,
    series: [
      { name: 'Read', type: 'line', showSymbol: false, data: history.map((sample) => sample.disk_read_bytes_per_second), itemStyle: { color: '#343741' } },
      { name: 'Write', type: 'line', showSymbol: false, data: history.map((sample) => sample.disk_write_bytes_per_second), itemStyle: { color: '#9170B8' } },
    ],
  };
  const unavailable = !host.reachable ? host.last_error || 'Host is unreachable.' : host.resource_observation_error;

  return <article className="host-resource-row">
    <div className="host-resource-summary">
      <div className="host-resource-summary__heading">
        <div><EuiTitle size="xs"><h3>{host.name}</h3></EuiTitle><EuiText size="s" color="subdued">Updated {timeAgo(latest?.observed_at || host.observed_at)}</EuiText></div>
        <EuiBadge color={host.reachable ? 'success' : 'danger'}>{host.reachable ? 'Live' : 'Unreachable'}</EuiBadge>
      </div>
      <div className="host-resource-clusters" aria-label={`${host.name} cluster membership`}>
        {host.clusters.map((cluster) => <span key={cluster.id} className="host-resource-cluster" style={{ '--item-accent': cluster.theme_color } as React.CSSProperties}>{cluster.name}</span>)}
      </div>
      <dl className="host-resource-current">
        <div><dt>CPU</dt><dd>{latest?.cpu_percent == null ? 'Awaiting sample' : `${latest.cpu_percent.toFixed(1)}%`}</dd></div>
        <div><dt>Memory</dt><dd>{memory == null ? 'Unavailable' : `${memory}%`}</dd></div>
        <div><dt>Network</dt><dd>{rate(latest?.network_rx_bytes_per_second)} in</dd></div>
        <div><dt>Disk I/O</dt><dd>{rate(latest?.disk_write_bytes_per_second)} write</dd></div>
      </dl>
      {unavailable && <EuiCallOut size="s" color="warning" title="Resource telemetry unavailable">{unavailable}</EuiCallOut>}
    </div>
    <div className="host-resource-charts">
      <div className="host-resource-chart" role="img" aria-label={`CPU and memory usage for ${host.name}`}><EuiText size="xs" color="subdued">CPU and memory</EuiText><ReactECharts option={resourceChart} style={{ height: 172 }} /></div>
      <div className="host-resource-chart" role="img" aria-label={`Network bandwidth for ${host.name}`}><EuiText size="xs" color="subdued">Network bandwidth</EuiText><ReactECharts option={networkChart} style={{ height: 172 }} /></div>
      <div className="host-resource-chart" role="img" aria-label={`Disk I/O for ${host.name}`}><EuiText size="xs" color="subdued">Disk I/O</EuiText><ReactECharts option={diskChart} style={{ height: 172 }} /></div>
    </div>
  </article>;
}

function ClusterOverview({ cluster, selected }: { cluster: ClusterSummary; selected: boolean }) {
  const setupRequired = cluster.node_count === 0 && cluster.workload_count === 0;
  const logs = monitoringState(cluster.log_monitoring);
  return (
    <article className={`cluster-overview ${selected ? 'is-selected' : ''}`} style={{ '--item-accent': cluster.theme_color } as React.CSSProperties}>
      <div className="cluster-overview__accent" />
      <div>
        <EuiFlexGroup justifyContent="spaceBetween" alignItems="center" responsive={false}>
          <EuiFlexItem><EuiTitle size="xs"><h3>{cluster.name}</h3></EuiTitle></EuiFlexItem>
          <EuiFlexItem grow={false}>{setupRequired ? <EuiBadge color="hollow">Setup needed</EuiBadge> : <HealthStatus health={cluster.health}>{healthLabel(cluster.health)}</HealthStatus>}</EuiFlexItem>
        </EuiFlexGroup>
        <EuiSpacer size="s" />
        <EuiText size="s" color="subdued">{setupRequired ? 'No hosts or workloads configured yet.' : `${cluster.node_count} hosts · ${cluster.workload_count} workloads · updated ${timeAgo(cluster.metrics?.observed_at)}`}</EuiText>
        {!setupRequired && <><EuiSpacer size="xs" /><EuiBadge color={monitoringColor(logs)}>Logs {logs}</EuiBadge></>}
      </div>
    </article>
  );
}

export function DashboardWorkspace() {
  const queryClient = useQueryClient();
  const { selectedClusterId, selectedCluster } = useConsole();
  const [nodeDetailsOpen, setNodeDetailsOpen] = useState(false);
  const { data, isLoading, error } = useQuery({ queryKey: ['dashboard'], queryFn: dashboardApi.snapshot, refetchInterval: 30000 });
  const { data: controllerSettings } = useQuery({ queryKey: ['controller-settings'], queryFn: dashboardApi.controllerSettings });
  const { data: topology } = useQuery({
    queryKey: ['topology', selectedClusterId],
    enabled: Boolean(selectedClusterId),
    queryFn: () => dashboardApi.topology(selectedClusterId!),
  });

  useEffect(() => {
    let source: EventSource | undefined;
    let cancelled = false;
    dashboardApi.streamToken().then(({ token }) => {
      if (cancelled) return;
      source = new EventSource(`/api/dashboard/events?token=${encodeURIComponent(token)}`);
      source.addEventListener('snapshot', (event) => queryClient.setQueryData(['dashboard'], JSON.parse((event as MessageEvent).data) as DashboardSnapshot));
      for (const name of ['host_stats', 'cluster_metrics', 'alert']) source.addEventListener(name, () => queryClient.invalidateQueries({ queryKey: ['dashboard'] }));
      source.addEventListener('run', () => queryClient.invalidateQueries({ queryKey: ['runs'] }));
    }).catch(() => undefined);
    return () => { cancelled = true; source?.close(); };
  }, [queryClient]);

  if (error) return <EuiCallOut title="Dashboard data is unavailable" color="danger" iconType="warning">{(error as Error).message}</EuiCallOut>;
  if (isLoading || !data) return <EuiProgress size="xs" color="accent" position="fixed" />;
  const cluster = data.clusters.find((item) => item.id === selectedClusterId);
  const setupRequired = Boolean(cluster && cluster.node_count === 0 && cluster.workload_count === 0);
  const awaitingData = cluster?.health === 'awaiting_data';
  const memberIds = new Set(selectedCluster?.members.map((member) => member.node_id) || []);
  const hosts = data.hosts.filter((host) => memberIds.has(host.id));
  const containers = hosts.flatMap((host) => host.containers.map((container) => ({ ...container, host: host.name })));
  const metrics: ClusterMetrics = cluster?.metrics || { cluster_id: selectedClusterId || 0, status: 'unknown' };
  const alerts = data.alerts.filter((alert) => alert.source === 'cluster' ? alert.source_id === selectedClusterId : memberIds.has(alert.source_id));
  const history = cluster?.history || [];
  const nodeBreakdown = metrics.node_breakdown || [];
  const zoneBreakdown = metrics.zone_breakdown || [];
  const accessUrls = topology?.access_urls || [];
  const logMonitoring = cluster?.log_monitoring || selectedCluster?.log_monitoring;
  const logsState = monitoringState(logMonitoring);
  const crossClusterHostUsage = data.cross_cluster_host_usage || [];
  const kibana = accessUrls.find((access) => access.role === 'kibana');
  const logsUrl = kibana && logMonitoring?.filebeat_enabled ? kibanaLogsUrl(kibana.url, cluster?.slug || selectedCluster?.slug || '') : undefined;
  const timezone = controllerSettings?.timezone || 'UTC';
  const chart = {
    animation: false,
    grid: { left: 45, right: 18, top: 24, bottom: 35 },
    tooltip: { trigger: 'axis' },
    legend: { data: ['Active shards', 'Unassigned shards'] },
    xAxis: { type: 'category', data: history.map((item) => formatTime(item.observed_at, timezone)) },
    yAxis: { type: 'value', minInterval: 1 },
    series: [
      { name: 'Active shards', type: 'line', data: history.map((item) => item.active_shards || 0), itemStyle: { color: cluster?.theme_color } },
      { name: 'Unassigned shards', type: 'line', data: history.map((item) => item.unassigned_shards || 0), itemStyle: { color: '#BD271E' } },
    ],
  };
  const columns = [
    { field: 'name', name: 'Workload' },
    { field: 'host', name: 'Host' },
    { field: 'state', name: 'State', render: (value: string) => <EuiBadge color={value === 'running' ? 'success' : 'danger'}>{value}</EuiBadge> },
    { field: 'cpu_percent', name: 'CPU', render: (value?: number) => `${(value || 0).toFixed(1)}%` },
    { field: 'memory_usage', name: 'Memory', render: (value: number, row: ContainerMetric) => `${bytes(value)} / ${bytes(row.memory_limit)}` },
    { field: 'network_rx', name: 'Network', render: (value: number, row: ContainerMetric) => `${bytes(value)} in / ${bytes(row.network_tx)} out` },
  ];
  const nodeColumns = [
    { field: 'name', name: 'Node' },
    { field: 'node_type', name: 'Type', render: (value: string) => <EuiBadge color="hollow">{value}</EuiBadge> },
    { field: 'zone', name: 'Zone', render: (value: string) => value ? <EuiBadge color="hollow">{value}</EuiBadge> : 'not assigned' },
    { field: 'roles', name: 'Elasticsearch roles', render: (value: string[]) => value.join(', ') || 'coordinating' },
    { field: 'shards', name: 'Shards' },
    { field: 'disk_used_bytes', name: 'Disk used', render: (value: number, row: NodeBreakdown) => `${bytes(value)} / ${bytes(row.disk_total_bytes)} (${percent(value, row.disk_total_bytes)}%)` },
    { field: 'heap_used_bytes', name: 'JVM heap', render: (value: number, row: NodeBreakdown) => `${bytes(value)} / ${bytes(row.heap_max_bytes)}` },
  ];
  const zoneColumns = [
    { field: 'zone', name: 'Availability zone', render: (value: string) => <EuiBadge color={value === 'unassigned' ? 'warning' : 'hollow'}>{value}</EuiBadge> },
    { field: 'nodes', name: 'Nodes' },
    { field: 'shards', name: 'Shards' },
    { field: 'disk_used_bytes', name: 'Disk used', render: (value: number, row: ZoneBreakdown) => `${bytes(value)} / ${bytes(row.disk_total_bytes)} (${percent(value, row.disk_total_bytes)}%)` },
    { field: 'heap_used_bytes', name: 'JVM heap', render: (value: number, row: ZoneBreakdown) => `${bytes(value)} / ${bytes(row.heap_max_bytes)}` },
  ];
  const accessColumns = [
    { field: 'label', name: 'Service' },
    { field: 'audience', name: 'Audience', render: (value: string) => <EuiBadge color="hollow">{value}</EuiBadge> },
    { field: 'url', name: 'Endpoint', render: (value: string) => <EuiLink href={value} target="_blank" rel="noreferrer">{value}</EuiLink> },
  ];

  return (
    <div className="page-stack">
      <div className="page-heading"><div><EuiTitle><h1>Dashboard</h1></EuiTitle><EuiText color="subdued">Live health across every managed Elastic Stack cluster.</EuiText></div><EuiBadge title={data.generated_at}>Updated {formatDateTime(data.generated_at, timezone)}</EuiBadge></div>
      <section className="cluster-overview-grid" aria-label="Cluster health overview">
        {data.clusters.map((item) => <ClusterOverview key={item.id} cluster={item} selected={item.id === selectedClusterId} />)}
        {!data.clusters.length && <EuiPanel className="dashboard-empty-state" paddingSize="l" hasBorder>
          <EuiTitle size="s"><h2>No clusters configured</h2></EuiTitle>
          <EuiSpacer size="s" />
          <EuiText color="subdued">Create a cluster before the dashboard can collect and display cluster health.</EuiText>
          <EuiSpacer size="m" />
          <EuiButton size="s" fill href="/clusters" iconType="plusInCircle">Create cluster</EuiButton>
        </EuiPanel>}
      </section>
      {cluster && <>
        {setupRequired ? <section className="section-band">
          <EuiPanel className="dashboard-setup-state" paddingSize="l" hasBorder>
            <EuiTitle size="s"><h2>Cluster setup not started</h2></EuiTitle>
            <EuiSpacer size="s" />
            <EuiText color="subdued">No hosts or workloads are configured for this cluster yet. Add host membership and assign a Master to start collecting health and capacity data.</EuiText>
            <EuiSpacer size="m" />
            <EuiButton size="s" fill href="/roles" iconType="arrowRight">Configure roles</EuiButton>
          </EuiPanel>
        </section> : <>
        <section className="section-band">
          <div className="section-heading"><div><EuiTitle size="s"><h2>{cluster.name}</h2></EuiTitle><HealthStatus health={cluster.health}>{healthLabel(cluster.health)}{awaitingData ? '' : ' cluster health'}</HealthStatus></div></div>
          {alerts.length > 0 && <div className="alert-stack">{alerts.map((alert, index) => <EuiCallOut key={`${alert.source}-${alert.source_id}-${index}`} size="s" title={alert.message} color={alert.severity === 'critical' ? 'danger' : 'warning'} iconType="warning" />)}</div>}
          {awaitingData ? <EuiPanel className="dashboard-setup-state" paddingSize="l" hasBorder>
            <EuiTitle size="s"><h2>Data role required</h2></EuiTitle>
            <EuiSpacer size="s" />
            <EuiText color="subdued">The Master is running. Assign a Hot data or Warm data workload before Elasticsearch can allocate the security index and publish cluster health.</EuiText>
            <EuiSpacer size="m" />
            <EuiButton size="s" fill href="/roles" iconType="arrowRight">Assign data role</EuiButton>
          </EuiPanel> : <div className="metric-grid">
            <Metric title="Elasticsearch nodes" value={metrics.nodes || 0} description={`${metrics.data_nodes || 0} data nodes`} />
            <Metric title="Indices" value={metrics.indices || 0} description={`${(metrics.documents || 0).toLocaleString()} documents`} />
            <Metric title="Active shards" value={metrics.active_shards || 0} description={`${metrics.unassigned_shards || 0} unassigned`} />
            <Metric title="Pending tasks" value={metrics.pending_tasks || 0} description={`Updated ${timeAgo(metrics.observed_at)}`} />
          </div>}
          {!awaitingData && <><EuiSpacer />
          <div className="split-band">
            <EuiPanel hasBorder paddingSize="m">
              <EuiTitle size="xs"><h3>Shard activity</h3></EuiTitle>
              {history.length ? <ReactECharts option={chart} style={{ height: 250 }} /> : <EuiText color="subdued">The chart fills as live observations arrive.</EuiText>}
            </EuiPanel>
            <EuiPanel hasBorder paddingSize="m">
              <EuiTitle size="xs"><h3>Capacity</h3></EuiTitle><EuiSpacer />
              <EuiText size="s">Disk used</EuiText>
              <EuiProgress value={percent((metrics.disk_total_bytes || 0) - (metrics.disk_available_bytes || 0), metrics.disk_total_bytes)} max={100} color="primary" size="l" label={`${bytes((metrics.disk_total_bytes || 0) - (metrics.disk_available_bytes || 0))} / ${bytes(metrics.disk_total_bytes)}`} valueText />
              <EuiSpacer />
              <EuiText size="s">JVM heap used</EuiText>
              <EuiProgress value={percent(metrics.heap_used_bytes, metrics.heap_max_bytes)} max={100} color="accent" size="l" label={`${bytes(metrics.heap_used_bytes)} / ${bytes(metrics.heap_max_bytes)}`} valueText />
              {metrics.last_error && <><EuiSpacer /><EuiCallOut size="s" color="warning" title="Metrics degraded">{metrics.last_error}</EuiCallOut></>}
            </EuiPanel>
          </div>
          {nodeBreakdown.length > 0 && <div className="node-breakdown">
            <div className="node-breakdown__heading">
              <div><EuiTitle size="xs"><h3>Node capacity and shard breakdown</h3></EuiTitle><EuiText size="s" color="subdued">Tier classification, allocated shards, disk, and JVM heap for each Elasticsearch node.</EuiText></div>
              <EuiButtonEmpty size="s" iconType={nodeDetailsOpen ? 'arrowUp' : 'arrowDown'} aria-expanded={nodeDetailsOpen} aria-controls="node-breakdown-table" aria-label={nodeDetailsOpen ? 'Hide node details' : 'Show node details'} onClick={() => setNodeDetailsOpen((open) => !open)}>{nodeDetailsOpen ? 'Hide details' : 'Details'}</EuiButtonEmpty>
            </div>
            {nodeDetailsOpen && <div id="node-breakdown-table" className="node-breakdown__table"><EuiBasicTable items={nodeBreakdown} columns={nodeColumns} tableLayout="auto" />{zoneBreakdown.length > 0 && <><EuiSpacer size="l" /><EuiTitle size="xs"><h3>Zone capacity and shard distribution</h3></EuiTitle><EuiSpacer size="s" /><EuiBasicTable items={zoneBreakdown} columns={zoneColumns} tableLayout="auto" /></>}</div>}
          </div>}</>}
        </section>
        <section className="section-band">
          <div className="section-heading"><div><EuiTitle size="s"><h2>User access endpoints</h2></EuiTitle><EuiText color="subdued">Configured user-facing services for the selected cluster.</EuiText></div><EuiFlexGroup gutterSize="s" alignItems="center" responsive={false}><EuiFlexItem grow={false}><EuiBadge color={monitoringColor(logsState)}>Log monitoring {logsState}</EuiBadge></EuiFlexItem>{logsUrl && <EuiFlexItem grow={false}><EuiButton size="s" iconType="search" href={logsUrl} target="_blank" rel="noreferrer">Open Kibana logs</EuiButton></EuiFlexItem>}</EuiFlexGroup></div>
          {accessUrls.length ? <EuiBasicTable items={accessUrls} columns={accessColumns} tableLayout="auto" /> : <EuiText color="subdued">No user-facing services are configured for this cluster.</EuiText>}
        </section>
        <section className="section-band">
          <div className="section-heading"><EuiTitle size="s"><h2>Workload utilization</h2></EuiTitle></div>
          <EuiBasicTable items={containers} columns={columns} tableLayout="auto" />
          {!containers.length && <EuiText color="subdued">No managed containers are reporting from the selected cluster.</EuiText>}
        </section>
        </>}
      </>}
      <section className="section-band" aria-label="Cross-cluster host resource usage">
        <div className="section-heading"><div><EuiTitle size="s"><h2>Cross-cluster host resource usage</h2></EuiTitle><EuiText color="subdued">Live host CPU, memory, network bandwidth, and physical disk I/O across cluster members.</EuiText></div></div>
        {crossClusterHostUsage.length ? <div className="host-resource-list">{crossClusterHostUsage.map((host) => <HostResourceUsage key={host.node_id} host={host} timezone={timezone} />)}</div> : <EuiText color="subdued">No cluster members are reporting resource telemetry yet.</EuiText>}
      </section>
    </div>
  );
}
