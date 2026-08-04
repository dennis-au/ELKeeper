import { EuiBadge, EuiCallOut, EuiSpacer, EuiText, EuiTitle } from '@elastic/eui';
import { useId } from 'react';
import type { MaintenancePredicateOutcome, MaintenancePredicateResult, MaintenanceTimestampFormatter } from './types';

const groups: Array<{
  outcome: MaintenancePredicateOutcome;
  title: string;
  color: 'success' | 'warning' | 'danger';
  iconType: string;
}> = [
  { outcome: 'blocking', title: 'Blocking conditions', color: 'danger', iconType: 'error' },
  { outcome: 'warning', title: 'Warnings', color: 'warning', iconType: 'warning' },
  { outcome: 'passed', title: 'Passed checks', color: 'success', iconType: 'check' },
];

export function MaintenancePredicateGroups({
  predicates,
  formatTimestamp,
}: {
  predicates: MaintenancePredicateResult[];
  formatTimestamp: MaintenanceTimestampFormatter;
}) {
  const headingId = useId();
  return (
    <section aria-labelledby={headingId}>
      <EuiTitle size="xs"><h3 id={headingId}>Safety checks</h3></EuiTitle>
      <EuiSpacer size="s" />
      {groups.map((group, index) => {
        const results = predicates.filter((predicate) => predicate.outcome === group.outcome);
        return <div key={group.outcome}>
          {index > 0 && <EuiSpacer size="s" />}
          <EuiCallOut
            size="s"
            color={group.color}
            iconType={group.iconType}
            title={<span>{group.title} <EuiBadge color={group.color}>{results.length}</EuiBadge></span>}
          >
            {results.length ? <EuiText size="s"><ul>
              {results.map((predicate) => <li key={predicate.id}>
                <strong>{predicate.title}</strong> <EuiBadge color="hollow">{predicate.id}</EuiBadge>
                <div>{predicate.evidence}</div>
                {predicate.remediation && <div><strong>Remediation:</strong> {predicate.remediation}</div>}
                <small>Observed {formatTimestamp(predicate.observedAt)}{predicate.forceable === false ? ' · cannot be overridden' : ''}</small>
              </li>)}
            </ul></EuiText> : <EuiText size="s" color="subdued"><p>No {group.title.toLowerCase()}.</p></EuiText>}
          </EuiCallOut>
        </div>;
      })}
    </section>
  );
}
