import {
  EuiBadge,
  EuiBasicTable,
  EuiCallOut,
  EuiDescriptionList,
  EuiHealth,
  EuiPanel,
  EuiSpacer,
  EuiText,
  EuiTitle,
} from '@elastic/eui';
import type {
  MaintenanceAvailability,
  MaintenanceDataTierImpact,
  MaintenanceImpact,
  MaintenanceImpactEndpoint,
} from './types';

const availabilityColor = {
  preserved: 'success',
  degraded: 'warning',
  unavailable: 'danger',
} as const;

function availabilityLabel(value: MaintenanceAvailability) {
  return value === 'preserved' ? 'Preserved' : value === 'degraded' ? 'Redundancy reduced' : 'Unavailable';
}

export function MaintenanceImpactSummary({ impact }: { impact: MaintenanceImpact }) {
  const unavailableEndpoints = impact.endpoints.filter((endpoint) => endpoint.availability === 'unavailable').length;
  const degradedEndpoints = impact.endpoints.filter((endpoint) => endpoint.availability === 'degraded').length;
  const endpointColumns = [
    { field: 'name', name: 'Endpoint' },
    {
      field: 'availability',
      name: 'Expected availability',
      render: (value: MaintenanceAvailability) => <EuiBadge color={availabilityColor[value]}>{availabilityLabel(value)}</EuiBadge>,
    },
    { field: 'detail', name: 'Impact', render: (value?: string) => value || 'No interruption expected' },
  ];
  const tierColumns = [
    { field: 'tier', name: 'Data tier' },
    { field: 'availableAfter', name: 'Available after plan', render: (value: number, row: MaintenanceDataTierImpact) => `${value} of ${row.total}` },
    { field: 'minimumRequired', name: 'Minimum required' },
    { field: 'safe', name: 'Budget', render: (value: boolean) => <EuiHealth color={value ? 'success' : 'danger'}>{value ? 'Preserved' : 'Violated'}</EuiHealth> },
  ];

  return (
    <EuiPanel hasBorder paddingSize="m">
      <EuiTitle size="xs"><h3>Planned impact</h3></EuiTitle>
      <EuiSpacer size="s" />
      <EuiDescriptionList
        compressed
        type="responsiveColumn"
        columnWidths={[1, 3]}
        listItems={[
          { title: 'Workloads', description: `${impact.workloads.length} affected across ${impact.clusters.length} cluster${impact.clusters.length === 1 ? '' : 's'}` },
          { title: 'Endpoints', description: `${impact.endpoints.length} assessed, ${degradedEndpoints} lose redundancy, ${unavailableEndpoints} unavailable` },
          {
            title: 'Master quorum',
            description: impact.masterQuorum
              ? <EuiHealth color={impact.masterQuorum.preserved ? 'success' : 'danger'}>{`${impact.masterQuorum.availableAfter} of ${impact.masterQuorum.total} remain; ${impact.masterQuorum.required} required`}</EuiHealth>
              : 'No master-eligible workload is affected',
          },
          {
            title: 'Data tiers',
            description: impact.dataTiers.length ? `${impact.dataTiers.length} active tier${impact.dataTiers.length === 1 ? '' : 's'} assessed` : 'No data tier is affected',
          },
          {
            title: 'Agents',
            description: impact.agents.affected
              ? `${impact.agents.affected} affected; ${impact.agents.interruptionExpected ? 'brief interruption expected' : 'no interruption expected'}`
              : 'No agents are affected',
          },
        ]}
      />
      {impact.singletonServices?.length ? <>
        <EuiSpacer size="m" />
        <EuiCallOut title="Singleton service interruption" color="warning" iconType="warning" size="s">
          <EuiText size="s"><ul>{impact.singletonServices.map((service) => <li key={service.name}><strong>{service.name}</strong>{service.estimatedOutage ? `: ${service.estimatedOutage}` : ': outage duration is not yet estimated'}</li>)}</ul></EuiText>
        </EuiCallOut>
      </> : null}
      {impact.endpoints.length ? <>
        <EuiSpacer size="m" />
        <EuiTitle size="xxs"><h4>Endpoint availability</h4></EuiTitle>
        <EuiSpacer size="s" />
        <EuiBasicTable items={impact.endpoints} columns={endpointColumns} tableLayout="auto" />
      </> : null}
      {impact.dataTiers.length ? <>
        <EuiSpacer size="m" />
        <EuiTitle size="xxs"><h4>Data-tier capacity</h4></EuiTitle>
        <EuiSpacer size="s" />
        <EuiBasicTable items={impact.dataTiers} columns={tierColumns} tableLayout="auto" />
      </> : null}
      {impact.workloads.length ? <>
        <EuiSpacer size="m" />
        <EuiText size="s" color="subdued">
          <p>Affected workloads: {impact.workloads.map((workload) => `${workload.name} (${workload.role} on ${workload.host})`).join(', ')}</p>
        </EuiText>
      </> : null}
    </EuiPanel>
  );
}
