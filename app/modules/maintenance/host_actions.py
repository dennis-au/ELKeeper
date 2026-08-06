"""Authenticated host-maintenance action contract.

This boundary dispatches only the existing host-scoped workflow. It owns no
transport, database table, or FastAPI concern.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any


class HostWorkflowAction(str, Enum):
    PREPARE = "prepare"
    STOP = "stop"
    REBOOT = "reboot"
    HANDOFF = "handoff"
    RETURN = "return"


class HostMaintenanceActionError(RuntimeError):
    """The host workflow action could not be completed safely."""


class HostMaintenanceActionDisabled(HostMaintenanceActionError):
    """The release capability has not approved host execution."""


@dataclass(frozen=True)
class HostMaintenanceActionResult:
    plan_id: str
    run_id: int
    action: str
    workflow_state: str
    lifecycle_state: str


class HostMaintenanceActionService:
    """Execute one named action through an injected host workflow.

    The capability is evaluated before opening a database connection or
    constructing a workflow, so a disabled release cannot acquire a transport
    or create a run as an accidental side effect.
    """

    def __init__(
        self,
        *,
        db_factory: Callable[[], Any],
        enabled: Callable[[], bool],
        workflow_factory: Callable[[Any], Any],
        close_workflow: Callable[[Any], Any] | None = None,
    ) -> None:
        self._db_factory = db_factory
        self._enabled = enabled
        self._workflow_factory = workflow_factory
        self._close_workflow = close_workflow

    async def perform(
        self,
        plan_id: str,
        action: HostWorkflowAction | str,
        *,
        username: str,
    ) -> HostMaintenanceActionResult:
        action = HostWorkflowAction(action)
        if not self._enabled():
            raise HostMaintenanceActionDisabled("Host maintenance execution is disabled")

        with self._db_factory() as connection:
            workflow = self._workflow_factory(connection)
            try:
                state = await self._dispatch(workflow, plan_id, action, username)
                plan = workflow.repository.get_plan(plan_id)
                if plan.id != plan_id or not isinstance(plan.run_id, int) or plan.run_id < 1:
                    raise HostMaintenanceActionError("Host maintenance workflow has no attached run")
                return HostMaintenanceActionResult(
                    plan_id=plan.id,
                    run_id=plan.run_id,
                    action=action.value,
                    workflow_state=state.workflow_state.value,
                    lifecycle_state=plan.lifecycle_state.value,
                )
            finally:
                if self._close_workflow is not None:
                    cleanup = self._close_workflow(workflow)
                    if inspect.isawaitable(cleanup):
                        await cleanup

    @staticmethod
    async def _dispatch(workflow: Any, plan_id: str, action: HostWorkflowAction, username: str):
        if action is HostWorkflowAction.PREPARE:
            return await workflow.prepare(plan_id, username=username)
        if action is HostWorkflowAction.STOP:
            return await workflow.stop_workloads(plan_id, username=username)
        if action is HostWorkflowAction.REBOOT:
            return await workflow.reboot_host(plan_id, username=username)
        if action is HostWorkflowAction.HANDOFF:
            return await workflow.record_operator_handoff(plan_id, username=username)
        return await workflow.return_to_service(plan_id, username=username)


__all__ = [
    "HostMaintenanceActionDisabled",
    "HostMaintenanceActionError",
    "HostMaintenanceActionResult",
    "HostMaintenanceActionService",
    "HostWorkflowAction",
]
