import { EuiCallOut, EuiSpacer } from '@elastic/eui';
import { MaintenanceImpactSummary } from './MaintenanceImpactSummary';
import { MaintenancePlanActions } from './MaintenancePlanActions';
import { MaintenancePlanHeader } from './MaintenancePlanHeader';
import { MaintenancePlanSteps } from './MaintenancePlanSteps';
import { MaintenancePredicateGroups } from './MaintenancePredicateGroups';
import type {
  MaintenanceAction,
  MaintenanceActionControls,
  MaintenancePlanState,
  MaintenancePlanViewModel,
  MaintenanceTimestampFormatter,
} from './types';

const statusPresentation: Record<MaintenancePlanState, { title: string; color: 'primary' | 'success' | 'warning' | 'danger'; iconType: string }> = {
  draft: { title: 'Plan draft', color: 'primary', iconType: 'document' },
  ready: { title: 'Plan ready', color: 'success', iconType: 'check' },
  blocked: { title: 'Plan blocked', color: 'danger', iconType: 'error' },
  executing: { title: 'Maintenance in progress', color: 'primary', iconType: 'play' },
  paused: { title: 'Maintenance paused', color: 'warning', iconType: 'pause' },
  recovery_required: { title: 'Recovery decision required', color: 'danger', iconType: 'warning' },
  succeeded: { title: 'Maintenance completed', color: 'success', iconType: 'check' },
  failed: { title: 'Maintenance failed', color: 'danger', iconType: 'error' },
  cancelled: { title: 'Maintenance cancelled', color: 'warning', iconType: 'stop' },
};

const defaultFormatTimestamp: MaintenanceTimestampFormatter = (value) => value || 'Not available';

export interface MaintenancePlanPreviewProps {
  plan: MaintenancePlanViewModel;
  actionControls?: MaintenanceActionControls;
  busyAction?: MaintenanceAction;
  onAction?: (action: MaintenanceAction) => void;
  formatTimestamp?: MaintenanceTimestampFormatter;
}

export function MaintenancePlanPreview({
  plan,
  actionControls,
  busyAction,
  onAction,
  formatTimestamp = defaultFormatTimestamp,
}: MaintenancePlanPreviewProps) {
  const presentation = statusPresentation[plan.header.state];
  const blockingCount = plan.predicates.filter((predicate) => predicate.outcome === 'blocking').length;
  const stale = plan.header.freshness.state !== 'fresh';
  const blockedByPredicate = blockingCount > 0;
  const statusDetail = plan.statusDetail || (blockedByPredicate
    ? `${blockingCount} blocking safety check${blockingCount === 1 ? '' : 's'} must be resolved before execution.`
    : plan.header.state === 'ready'
      ? 'All current safety checks pass. Execution still revalidates the plan immediately before each protected side effect.'
      : 'The persisted plan state and verified checkpoints determine the next valid action.');
  const calloutTitle = stale ? 'Plan requires refresh' : blockedByPredicate ? 'Plan blocked' : presentation.title;
  const calloutColor = stale ? 'warning' : blockedByPredicate ? 'danger' : presentation.color;
  const calloutIcon = stale ? 'refresh' : blockedByPredicate ? 'error' : presentation.iconType;

  return (
    <div aria-label={`Maintenance plan ${plan.header.planId}`}>
      <EuiCallOut title={calloutTitle} color={calloutColor} iconType={calloutIcon}>
        {stale ? `${statusDetail} Planning observations are ${plan.header.freshness.state}; execution is disabled.` : statusDetail}
      </EuiCallOut>
      <EuiSpacer size="m" />
      <MaintenancePlanHeader header={plan.header} formatTimestamp={formatTimestamp} />
      <EuiSpacer size="m" />
      <MaintenanceImpactSummary impact={plan.impact} />
      <EuiSpacer size="m" />
      <MaintenancePredicateGroups predicates={plan.predicates} formatTimestamp={formatTimestamp} />
      <EuiSpacer size="m" />
      <MaintenancePlanSteps steps={plan.steps} lastVerifiedCheckpoint={plan.lastVerifiedCheckpoint} formatTimestamp={formatTimestamp} />
      <EuiSpacer size="m" />
      <MaintenancePlanActions
        controls={actionControls}
        planState={plan.header.state}
        freshness={plan.header.freshness.state}
        hasBlockingPredicates={blockingCount > 0}
        busyAction={busyAction}
        onAction={onAction}
      />
    </div>
  );
}
