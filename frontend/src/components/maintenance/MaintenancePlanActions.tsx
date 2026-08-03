import { EuiButton, EuiButtonEmpty, EuiFlexGroup, EuiFlexItem, EuiToolTip } from '@elastic/eui';
import type {
  MaintenanceAction,
  MaintenanceActionControls,
  MaintenanceFreshnessState,
  MaintenancePlanState,
} from './types';

const defaultLabels: Record<MaintenanceAction, string> = {
  execute: 'Execute plan',
  pause: 'Pause',
  resume: 'Resume',
  cancel: 'Cancel plan',
  recover: 'Review recovery',
};

const stateAllowsAction: Record<MaintenanceAction, MaintenancePlanState[]> = {
  execute: ['ready'],
  pause: ['executing'],
  resume: ['paused'],
  cancel: ['draft', 'ready', 'blocked', 'executing', 'paused', 'recovery_required'],
  recover: ['recovery_required'],
};

export function MaintenancePlanActions({
  controls,
  planState,
  freshness,
  hasBlockingPredicates,
  busyAction,
  onAction,
}: {
  controls?: MaintenanceActionControls;
  planState: MaintenancePlanState;
  freshness: MaintenanceFreshnessState;
  hasBlockingPredicates: boolean;
  busyAction?: MaintenanceAction;
  onAction?: (action: MaintenanceAction) => void;
}) {
  const actions = (Object.keys(defaultLabels) as MaintenanceAction[]).filter((action) => {
    const control = controls?.[action];
    return control && control.visible !== false;
  });
  if (!actions.length) return null;

  return (
    <EuiFlexGroup gutterSize="s" justifyContent="flexEnd" wrap responsive={false} aria-label="Maintenance plan actions">
      {actions.map((action) => {
        const control = controls![action]!;
        const lifecycleAllowed = stateAllowsAction[action].includes(planState);
        const executionBlocked = action === 'execute' && (freshness !== 'fresh' || hasBlockingPredicates);
        const disabled = !control.enabled || !onAction || !lifecycleAllowed || executionBlocked || Boolean(busyAction);
        const reason = control.reason
          || (!lifecycleAllowed ? `This action is unavailable while the plan is ${planState.replaceAll('_', ' ')}.` : undefined)
          || (action === 'execute' && freshness !== 'fresh' ? 'Refresh and re-plan before execution.' : undefined)
          || (action === 'execute' && hasBlockingPredicates ? 'Resolve every blocking safety check before execution.' : undefined)
          || (!control.enabled ? 'The controller has not enabled this action for the current plan.' : undefined)
          || (!onAction ? 'No action handler is connected.' : undefined);
        const button = action === 'execute'
          ? <EuiButton fill iconType="play" disabled={disabled} isLoading={busyAction === action} onClick={() => onAction?.(action)}>{control.label || defaultLabels[action]}</EuiButton>
          : <EuiButtonEmpty disabled={disabled} isLoading={busyAction === action} onClick={() => onAction?.(action)}>{control.label || defaultLabels[action]}</EuiButtonEmpty>;
        return <EuiFlexItem key={action} grow={false}>{reason && disabled ? <EuiToolTip content={reason}>{button}</EuiToolTip> : button}</EuiFlexItem>;
      })}
    </EuiFlexGroup>
  );
}
