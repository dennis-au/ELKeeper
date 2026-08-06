import { EuiButton, EuiFlexGroup, EuiFlexItem, EuiText } from '@elastic/eui';

export type MaintenanceWorkflowAction = 'prepare' | 'stop' | 'reboot' | 'return';

export interface MaintenanceWorkflowActionControl {
  enabled: boolean;
  label: string;
  reason?: string;
}

export interface MaintenanceWorkflowActionsProps {
  control?: MaintenanceWorkflowActionControl & { action: MaintenanceWorkflowAction };
  busyAction?: MaintenanceWorkflowAction;
  onAction?: (action: MaintenanceWorkflowAction) => void;
}

export function MaintenanceWorkflowActions({
  control,
  busyAction,
  onAction,
}: MaintenanceWorkflowActionsProps) {
  if (!control) return null;

  const disabled = !control.enabled || Boolean(busyAction) || !onAction;
  const reason = control.reason
    || (!control.enabled ? 'The corresponding maintenance capability is disabled in this release.' : undefined)
    || (!onAction ? 'No workflow action handler is connected.' : undefined)
    || (busyAction ? 'Another maintenance request is in progress.' : undefined);
  const destructive = control.action === 'stop' || control.action === 'reboot';

  return (
    <div>
      <EuiFlexGroup
        aria-label="Maintenance workflow action"
        role="group"
        alignItems="center"
        justifyContent="flexEnd"
        gutterSize="s"
        responsive={false}
        wrap
      >
        <EuiFlexItem grow={false}>
          <EuiButton
            fill={control.action === 'prepare' || control.action === 'return'}
            color={destructive ? 'danger' : 'primary'}
            iconType={control.action === 'prepare' ? 'wrench' : control.action === 'stop' ? 'stop' : control.action === 'reboot' ? 'refresh' : 'play'}
            disabled={disabled}
            isLoading={busyAction === control.action}
            onClick={() => onAction?.(control.action)}
            aria-describedby={reason && disabled ? 'maintenance-workflow-action-reason' : undefined}
          >
            {control.label}
          </EuiButton>
        </EuiFlexItem>
      </EuiFlexGroup>
      {reason && disabled ? <EuiText size="xs" color="subdued"><p id="maintenance-workflow-action-reason">{reason}</p></EuiText> : null}
    </div>
  );
}
