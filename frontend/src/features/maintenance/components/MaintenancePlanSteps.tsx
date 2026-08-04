import { EuiBadge, EuiSpacer, EuiSteps, EuiText, EuiTitle } from '@elastic/eui';
import type { EuiStepStatus } from '@elastic/eui';
import { useId } from 'react';
import type { MaintenancePlanStep, MaintenanceStepState, MaintenanceTimestampFormatter } from './types';

const stepStatus: Record<MaintenanceStepState, EuiStepStatus> = {
  pending: 'incomplete',
  active: 'current',
  completed: 'complete',
  blocked: 'warning',
  failed: 'danger',
  paused: 'warning',
  recovery_required: 'danger',
  skipped: 'disabled',
};

const stepBadgeColor = {
  pending: 'hollow',
  active: 'primary',
  completed: 'success',
  blocked: 'warning',
  failed: 'danger',
  paused: 'warning',
  recovery_required: 'danger',
  skipped: 'hollow',
} as const;

function stateLabel(value: MaintenanceStepState) {
  return value.replaceAll('_', ' ');
}

export function MaintenancePlanSteps({
  steps,
  lastVerifiedCheckpoint,
  formatTimestamp,
}: {
  steps: MaintenancePlanStep[];
  lastVerifiedCheckpoint?: string;
  formatTimestamp: MaintenanceTimestampFormatter;
}) {
  const headingId = useId();
  const ordered = [...steps].sort((left, right) => left.sequence - right.sequence);
  return (
    <section aria-labelledby={headingId}>
      <EuiTitle size="xs"><h3 id={headingId}>Ordered steps and checkpoints</h3></EuiTitle>
      {lastVerifiedCheckpoint && <EuiText size="s" color="subdued"><p>Last verified checkpoint: <strong>{lastVerifiedCheckpoint}</strong></p></EuiText>}
      <EuiSpacer size="s" />
      {ordered.length ? <EuiSteps
        headingElement="h4"
        titleSize="xs"
        steps={ordered.map((step) => ({
          title: step.title,
          status: stepStatus[step.state],
          children: <EuiText size="s">
            <p>{step.description}</p>
            <p><EuiBadge color={stepBadgeColor[step.state]}>{stateLabel(step.state)}</EuiBadge>{step.target ? ` Target: ${step.target}` : ''}</p>
            {step.checkpoint && <p><strong>Checkpoint:</strong> {step.checkpoint.label}{step.checkpoint.verifiedAt ? ` · verified ${formatTimestamp(step.checkpoint.verifiedAt)}` : ' · not yet verified'}</p>}
          </EuiText>,
        }))}
      /> : <EuiText size="s" color="subdued"><p>No execution steps have been compiled.</p></EuiText>}
    </section>
  );
}
