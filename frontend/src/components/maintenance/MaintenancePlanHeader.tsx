import {
  EuiBadge,
  EuiDescriptionList,
  EuiFlexGroup,
  EuiFlexItem,
  EuiHealth,
  EuiPanel,
  EuiText,
  EuiTitle,
} from '@elastic/eui';
import type { MaintenancePlanHeaderData, MaintenanceTimestampFormatter } from './types';

const stateColor = {
  draft: 'hollow',
  ready: 'success',
  blocked: 'danger',
  executing: 'primary',
  paused: 'warning',
  recovery_required: 'danger',
  succeeded: 'success',
  failed: 'danger',
  cancelled: 'hollow',
} as const;

const freshnessColor = {
  fresh: 'success',
  stale: 'warning',
  expired: 'danger',
} as const;

function label(value: string) {
  return value.replaceAll('_', ' ').replace(/\b\w/g, (character) => character.toUpperCase());
}

export function MaintenancePlanHeader({
  header,
  formatTimestamp,
}: {
  header: MaintenancePlanHeaderData;
  formatTimestamp: MaintenanceTimestampFormatter;
}) {
  const freshness = header.freshness;
  const freshnessText = freshness.state === 'fresh'
    ? 'Plan observations are current'
    : freshness.state === 'stale'
      ? 'Plan observations are stale'
      : 'Plan approval window has expired';

  return (
    <EuiPanel hasBorder paddingSize="m">
      <EuiFlexGroup alignItems="flexStart" justifyContent="spaceBetween" gutterSize="m">
        <EuiFlexItem>
          <EuiTitle size="s"><h2>{header.target.name} maintenance plan</h2></EuiTitle>
          <EuiText size="s" color="subdued">
            <p>{label(header.target.kind)} plan <strong>{header.planId}</strong></p>
          </EuiText>
        </EuiFlexItem>
        <EuiFlexItem grow={false}>
          <EuiFlexGroup responsive={false} wrap gutterSize="s" alignItems="center">
            <EuiFlexItem grow={false}><EuiBadge color={stateColor[header.state]}>{label(header.state)}</EuiBadge></EuiFlexItem>
            <EuiFlexItem grow={false}><EuiHealth color={freshnessColor[freshness.state]}>{freshnessText}</EuiHealth></EuiFlexItem>
          </EuiFlexGroup>
        </EuiFlexItem>
      </EuiFlexGroup>
      <EuiDescriptionList
        compressed
        type="responsiveColumn"
        columnWidths={[1, 3]}
        listItems={[
          { title: 'Operation', description: header.operation },
          { title: 'Reason', description: header.reason },
          { title: 'Requested by', description: header.requester },
          { title: 'Created', description: formatTimestamp(header.createdAt) },
          { title: 'Policy', description: `${header.policy.name} revision ${header.policy.revision} (${header.policy.availabilityMode})` },
          { title: 'Observed', description: formatTimestamp(freshness.observedAt) },
          { title: 'Expires', description: formatTimestamp(freshness.expiresAt) },
        ]}
      />
      {freshness.detail && <EuiText size="s" color="subdued"><p>{freshness.detail}</p></EuiText>}
    </EuiPanel>
  );
}
