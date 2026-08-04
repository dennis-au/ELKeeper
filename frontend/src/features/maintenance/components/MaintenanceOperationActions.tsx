import {
  EuiButton,
  EuiButtonEmpty,
  EuiCallOut,
  EuiFlexGroup,
  EuiFlexItem,
  EuiText,
} from '@elastic/eui';
import { useId } from 'react';
import type { MaintenancePlanState } from './types';

export type MaintenanceOperationAction = 'pause' | 'resume' | 'cancel' | 'recover';

export interface MaintenanceOperationActionControl {
  visible?: boolean;
  enabled: boolean;
  label?: string;
  reason?: string;
  requiresSafeCheckpoint?: boolean;
}

export type MaintenanceOperationActionControls = Partial<Record<MaintenanceOperationAction, MaintenanceOperationActionControl>>;

const actionOrder: MaintenanceOperationAction[] = ['pause', 'resume', 'recover', 'cancel'];

const defaultLabels: Record<MaintenanceOperationAction, string> = {
  pause: 'Pause after checkpoint',
  resume: 'Resume maintenance',
  cancel: 'Cancel maintenance',
  recover: 'Recover operation',
};

const lifecycleStates: Record<MaintenanceOperationAction, MaintenancePlanState[]> = {
  pause: ['executing'],
  resume: ['paused'],
  cancel: ['draft', 'ready', 'blocked', 'executing', 'paused', 'recovery_required'],
  recover: ['recovery_required'],
};

export interface MaintenanceOperationActionsProps {
  lifecycleState: MaintenancePlanState;
  safeCheckpoint: boolean;
  safeCheckpointReason?: string;
  controls?: MaintenanceOperationActionControls;
  busyAction?: MaintenanceOperationAction;
  onAction?: (action: MaintenanceOperationAction) => void;
}

export function MaintenanceOperationActions({
  lifecycleState,
  safeCheckpoint,
  safeCheckpointReason,
  controls,
  busyAction,
  onAction,
}: MaintenanceOperationActionsProps) {
  const reasonPrefix = useId();
  const checkpointReasonId = `${reasonPrefix}-checkpoint`;
  const visibleActions = actionOrder.filter((action) => {
    const control = controls?.[action];
    return Boolean(control && control.visible !== false && lifecycleStates[action].includes(lifecycleState));
  });
  if (!visibleActions.length) return null;

  const waitingForCheckpoint = visibleActions.some((action) => {
    const control = controls?.[action];
    return control?.requiresSafeCheckpoint !== false && !safeCheckpoint;
  });

  return (
    <div>
      {waitingForCheckpoint ? <>
        <EuiCallOut title="Waiting for a safe checkpoint" color="warning" iconType="pause" size="s">
          <span id={checkpointReasonId}>{safeCheckpointReason || 'The active side effect must finish before operator actions can be applied.'}</span>
        </EuiCallOut>
      </> : null}
      <EuiFlexGroup
        aria-label="Maintenance operation actions"
        role="group"
        alignItems="flexStart"
        justifyContent="flexEnd"
        gutterSize="s"
        wrap
        responsive={false}
      >
        {visibleActions.map((action) => {
          const control = controls![action]!;
          const requiresSafeCheckpoint = control.requiresSafeCheckpoint !== false;
          const checkpointBlocked = requiresSafeCheckpoint && !safeCheckpoint;
          const disabled = checkpointBlocked || !control.enabled || !onAction || Boolean(busyAction);
          const reason = checkpointBlocked
            ? safeCheckpointReason || 'This action requires a verified safe checkpoint.'
            : control.reason
              || (!control.enabled ? 'The controller has not authorized this action for the current evidence.' : undefined)
              || (!onAction ? 'No action handler is connected.' : undefined)
              || (busyAction ? 'Another maintenance request is in progress.' : undefined);
          const reasonId = checkpointBlocked ? checkpointReasonId : `${reasonPrefix}-${action}`;
          const buttonProps = {
            disabled,
            isLoading: busyAction === action,
            onClick: () => onAction?.(action),
            'aria-describedby': reason && disabled ? reasonId : undefined,
          };
          const button = action === 'resume' || action === 'recover'
            ? <EuiButton {...buttonProps} fill iconType={action === 'resume' ? 'play' : 'refresh'}>{control.label || defaultLabels[action]}</EuiButton>
            : <EuiButtonEmpty {...buttonProps} color={action === 'cancel' ? 'danger' : 'primary'} iconType={action === 'pause' ? 'pause' : 'stop'}>{control.label || defaultLabels[action]}</EuiButtonEmpty>;

          return <EuiFlexItem key={action} grow={false}>
            {button}
            {reason && disabled && !checkpointBlocked ? <EuiText size="xs" color="subdued"><p id={reasonId}>{reason}</p></EuiText> : null}
          </EuiFlexItem>;
        })}
      </EuiFlexGroup>
    </div>
  );
}
