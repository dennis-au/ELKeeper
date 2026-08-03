import {
  EuiBadge,
  EuiCallOut,
  EuiDescriptionList,
  EuiHealth,
  EuiPanel,
  EuiProgress,
  EuiSpacer,
  EuiText,
  EuiTitle,
} from '@elastic/eui';
import type { MaintenancePlanState, MaintenanceTimestampFormatter } from './types';

export type MaintenanceCheckpointEvidenceState =
  | 'pending'
  | 'active'
  | 'verified'
  | 'blocked'
  | 'recovery_required';

export type MaintenanceHostBootState =
  | 'not_started'
  | 'reboot_requested'
  | 'waiting_for_return'
  | 'returned'
  | 'verified'
  | 'unavailable'
  | 'unknown';

export type MaintenanceCleanupState = 'not_required' | 'pending' | 'restored' | 'unresolved';
export type MaintenanceExecutorState = 'not_staged' | 'staged' | 'running' | 'complete' | 'recovery_required' | 'unavailable';
export type MaintenanceExecutorCheckState = 'pending' | 'passed' | 'failed';

export interface MaintenanceCheckpointEvidence {
  id: string;
  label: string;
  state: MaintenanceCheckpointEvidenceState;
  safeForOperatorAction: boolean;
  detail?: string;
  updatedAt?: string;
}

export interface MaintenanceHostBootEvidence {
  state: MaintenanceHostBootState;
  bootTransitionVerified: boolean;
  observedAt?: string;
  detail?: string;
}

export interface MaintenanceCleanupEvidence {
  id: string;
  kind: 'allocation' | 'shutdown';
  clusterName: string;
  state: MaintenanceCleanupState;
  detail?: string;
  updatedAt?: string;
}

export interface MaintenanceExecutorCheckEvidence {
  id: string;
  label: string;
  state: MaintenanceExecutorCheckState;
  detail?: string;
}

export interface MaintenanceExecutorEvidence {
  state: MaintenanceExecutorState;
  signatureVerified?: boolean;
  resultIdentityVerified?: boolean;
  resultImported?: boolean;
  reason?: string;
  observedAt?: string;
  checks?: MaintenanceExecutorCheckEvidence[];
}

export interface MaintenanceOperationProgressModel {
  lifecycleState: MaintenancePlanState;
  progress?: { completed: number; total: number };
  activeCheckpoint?: MaintenanceCheckpointEvidence;
  lastVerifiedCheckpoint?: { label: string; verifiedAt?: string };
  hostBoot: MaintenanceHostBootEvidence;
  cleanup: MaintenanceCleanupEvidence[];
  executor: MaintenanceExecutorEvidence;
}

const checkpointColor = {
  pending: 'hollow',
  active: 'primary',
  verified: 'success',
  blocked: 'warning',
  recovery_required: 'danger',
} as const;

const bootColor = {
  not_started: 'subdued',
  reboot_requested: 'primary',
  waiting_for_return: 'warning',
  returned: 'primary',
  verified: 'success',
  unavailable: 'danger',
  unknown: 'subdued',
} as const;

const cleanupColor = {
  not_required: 'subdued',
  pending: 'warning',
  restored: 'success',
  unresolved: 'danger',
} as const;

const executorColor = {
  not_staged: 'subdued',
  staged: 'primary',
  running: 'primary',
  complete: 'success',
  recovery_required: 'danger',
  unavailable: 'subdued',
} as const;

const checkColor = {
  pending: 'subdued',
  passed: 'success',
  failed: 'danger',
} as const;

const defaultFormatTimestamp: MaintenanceTimestampFormatter = (value) => value || 'Not available';

function label(value: string) {
  return value.replaceAll('_', ' ').replace(/\b\w/g, (character) => character.toUpperCase());
}

export function MaintenanceOperationProgress({
  progress,
  formatTimestamp = defaultFormatTimestamp,
}: {
  progress: MaintenanceOperationProgressModel;
  formatTimestamp?: MaintenanceTimestampFormatter;
}) {
  const unresolvedCleanup = progress.cleanup.filter((item) => item.state === 'unresolved');
  const needsRecovery = progress.lifecycleState === 'recovery_required' || unresolvedCleanup.length > 0;
  const completed = Math.max(0, progress.progress?.completed || 0);
  const total = Math.max(completed, progress.progress?.total || 0);

  return (
    <section aria-live="polite" aria-label="Maintenance operation progress">
      <EuiPanel hasBorder paddingSize="m">
        <EuiTitle size="xs"><h3>Operation progress and recovery evidence</h3></EuiTitle>
        <EuiText size="s" color="subdued">
          <p>Persisted checkpoints and independently verified host evidence determine which operator actions are safe.</p>
        </EuiText>
        {progress.progress && total > 0 ? <>
          <EuiProgress value={completed} max={total} color={needsRecovery ? 'warning' : 'primary'} size="m" />
          <EuiText size="s"><p>{completed} of {total} steps complete</p></EuiText>
        </> : null}
        {needsRecovery ? <>
          <EuiSpacer size="s" />
          <EuiCallOut title="Recovery evidence needs attention" color="danger" iconType="warning" size="s">
            {unresolvedCleanup.length
              ? `${unresolvedCleanup.length} temporary Elasticsearch cleanup item${unresolvedCleanup.length === 1 ? ' remains' : 's remain'} unresolved.`
              : 'The operation requires an explicit recovery decision before further disruption.'}
          </EuiCallOut>
        </> : null}

        <EuiSpacer size="m" />
        <EuiTitle size="xxs"><h4>Checkpoint</h4></EuiTitle>
        <EuiSpacer size="xs" />
        {progress.activeCheckpoint ? <EuiDescriptionList
          compressed
          type="responsiveColumn"
          columnWidths={[1, 3]}
          listItems={[
            { title: 'Active checkpoint', description: progress.activeCheckpoint.label },
            { title: 'Checkpoint state', description: <EuiBadge color={checkpointColor[progress.activeCheckpoint.state]}>{label(progress.activeCheckpoint.state)}</EuiBadge> },
            { title: 'Operator boundary', description: <EuiHealth color={progress.activeCheckpoint.safeForOperatorAction ? 'success' : 'warning'}>{progress.activeCheckpoint.safeForOperatorAction ? 'Safe checkpoint verified' : 'Action boundary not verified'}</EuiHealth> },
            { title: 'Evidence updated', description: formatTimestamp(progress.activeCheckpoint.updatedAt) },
            ...(progress.lastVerifiedCheckpoint ? [{
              title: 'Last verified checkpoint',
              description: <>{progress.lastVerifiedCheckpoint.label}{progress.lastVerifiedCheckpoint.verifiedAt ? ` at ${formatTimestamp(progress.lastVerifiedCheckpoint.verifiedAt)}` : ''}</>,
            }] : []),
          ]}
        /> : <EuiText size="s" color="subdued"><p>Checkpoint evidence has not been recorded yet.</p></EuiText>}
        {progress.activeCheckpoint?.detail ? <EuiText size="s"><p>{progress.activeCheckpoint.detail}</p></EuiText> : null}

        <EuiSpacer size="m" />
        <EuiTitle size="xxs"><h4>Host boot state</h4></EuiTitle>
        <EuiSpacer size="xs" />
        <EuiDescriptionList
          compressed
          type="responsiveColumn"
          columnWidths={[1, 3]}
          listItems={[
            {
              title: 'Observed state',
              description: <EuiHealth color={bootColor[progress.hostBoot.state]}>{progress.hostBoot.state === 'unknown' ? 'Host boot state is unknown' : label(progress.hostBoot.state)}</EuiHealth>,
            },
            {
              title: 'Boot identity',
              description: <EuiHealth color={progress.hostBoot.bootTransitionVerified ? 'success' : 'warning'}>{progress.hostBoot.bootTransitionVerified ? 'Boot transition verified' : 'Boot transition not verified'}</EuiHealth>,
            },
            { title: 'Observed', description: formatTimestamp(progress.hostBoot.observedAt) },
          ]}
        />
        {progress.hostBoot.detail ? <EuiText size="s"><p>{progress.hostBoot.detail}</p></EuiText> : null}

        <EuiSpacer size="m" />
        <EuiTitle size="xxs"><h4>Temporary Elasticsearch cleanup</h4></EuiTitle>
        <EuiSpacer size="xs" />
        {progress.cleanup.length ? <EuiText size="s">
          <ul aria-label="Temporary cleanup evidence">
            {progress.cleanup.map((item) => <li key={item.id}>
              <strong>{item.clusterName}</strong>: {item.kind === 'allocation' ? 'allocation setting' : 'shutdown record'}{' '}
              <EuiBadge color={cleanupColor[item.state]}>{label(item.state)}</EuiBadge>
              {item.detail ? <>. {item.detail}</> : null}
              {item.updatedAt ? <> Observed {formatTimestamp(item.updatedAt)}.</> : null}
            </li>)}
          </ul>
        </EuiText> : <EuiText size="s" color="subdued"><p>No temporary allocation or shutdown cleanup is recorded.</p></EuiText>}

        <EuiSpacer size="m" />
        <EuiTitle size="xxs"><h4>One-shot executor evidence</h4></EuiTitle>
        <EuiSpacer size="xs" />
        {progress.executor.state === 'unavailable' ? <EuiText size="s" color="subdued"><p>Executor evidence is unavailable</p></EuiText> : <>
          <EuiDescriptionList
            compressed
            type="responsiveColumn"
            columnWidths={[1, 3]}
            listItems={[
              { title: 'Executor state', description: <EuiHealth color={executorColor[progress.executor.state]}>{label(progress.executor.state)}</EuiHealth> },
              { title: 'Manifest signature', description: progress.executor.signatureVerified === true ? 'Manifest signature verified' : progress.executor.signatureVerified === false ? 'Manifest signature not verified' : 'Manifest signature not reported' },
              { title: 'Result identity', description: progress.executor.resultIdentityVerified === true ? 'Executor result identity verified' : progress.executor.resultIdentityVerified === false ? 'Executor result identity not verified' : 'Executor result identity not reported' },
              { title: 'Controller import', description: progress.executor.resultImported === true ? 'Result imported' : progress.executor.resultImported === false ? 'Result not imported' : 'Import state not reported' },
              { title: 'Observed', description: formatTimestamp(progress.executor.observedAt) },
            ]}
          />
          {progress.executor.reason ? <EuiText size="s"><p>{progress.executor.reason}</p></EuiText> : null}
          {progress.executor.checks?.length ? <EuiText size="s">
            <ul aria-label="Executor checks">
              {progress.executor.checks.map((check) => <li key={check.id}>
                <EuiHealth color={checkColor[check.state]}>{check.label}</EuiHealth>
                {check.detail ? `: ${check.detail}` : ''}
              </li>)}
            </ul>
          </EuiText> : null}
        </>}
      </EuiPanel>
    </section>
  );
}
