"""HTTP contract for assignment-scoped planned maintenance actions."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from fastapi import APIRouter, Depends, HTTPException

from .container_actions import (
    ContainerMaintenanceActionDisabled,
    ContainerWorkflowAction,
)
from .container_maintenance import ContainerMaintenanceError
from .host_actions import HostMaintenanceActionDisabled, HostWorkflowAction
from .host_maintenance import HostMaintenanceError
from .execution import MaintenanceExecutionError
from .store import (
    LockConflict,
    LockOwnershipError,
    OverlappingPlanError,
    RecordNotFound,
    RevisionConflict,
    StaleLockRequiresRecovery,
)


class ContainerWorkflowOperations(Protocol):
    async def perform(self, plan_id: str, action: ContainerWorkflowAction, *, username: str): ...


class HostWorkflowOperations(Protocol):
    async def perform(self, plan_id: str, action: HostWorkflowAction, *, username: str): ...


def build_container_workflow_router(
    *,
    operations: ContainerWorkflowOperations,
    user_dependency: Callable,
) -> APIRouter:
    """Build the isolated action API without widening legacy reboot routes."""

    router = APIRouter()

    @router.post("/api/maintenance/workflows/{plan_id}/{action}")
    async def run_container_workflow_action(
        plan_id: str,
        action: ContainerWorkflowAction,
        username: str = Depends(user_dependency),
    ):
        try:
            result = await operations.perform(plan_id, action, username=username)
        except ContainerMaintenanceActionDisabled as error:
            raise HTTPException(
                409,
                "Container maintenance execution is disabled until its execution safety gate passes",
            ) from error
        except RecordNotFound as error:
            raise HTTPException(404, str(error)) from error
        except (
            ContainerMaintenanceError,
            MaintenanceExecutionError,
            LockConflict,
            LockOwnershipError,
            OverlappingPlanError,
            RevisionConflict,
            StaleLockRequiresRecovery,
            ValueError,
        ) as error:
            raise HTTPException(409, str(error)) from error
        except Exception as error:
            raise HTTPException(
                502,
                "Container maintenance action stopped at a protected boundary; recovery is required",
            ) from error
        return {
            "plan_id": result.plan_id,
            "run_id": result.run_id,
            "action": result.action,
            "workflow_state": result.workflow_state,
            "lifecycle_state": result.lifecycle_state,
        }

    return router


def build_host_workflow_router(
    *,
    operations: HostWorkflowOperations,
    user_dependency: Callable,
) -> APIRouter:
    """Build the isolated host action API without widening legacy reboot routes."""

    router = APIRouter()

    @router.post("/api/maintenance/host-workflows/{plan_id}/{action}")
    async def run_host_workflow_action(
        plan_id: str,
        action: HostWorkflowAction,
        username: str = Depends(user_dependency),
    ):
        try:
            result = await operations.perform(plan_id, action, username=username)
        except HostMaintenanceActionDisabled as error:
            raise HTTPException(
                409,
                "Host maintenance execution is disabled until its execution safety gate passes",
            ) from error
        except RecordNotFound as error:
            raise HTTPException(404, str(error)) from error
        except (
            HostMaintenanceError,
            MaintenanceExecutionError,
            LockConflict,
            LockOwnershipError,
            OverlappingPlanError,
            RevisionConflict,
            StaleLockRequiresRecovery,
            ValueError,
        ) as error:
            raise HTTPException(409, str(error)) from error
        except Exception as error:
            raise HTTPException(
                502,
                "Host maintenance action stopped at a protected boundary; recovery is required",
            ) from error
        return {
            "plan_id": result.plan_id,
            "run_id": result.run_id,
            "action": result.action,
            "workflow_state": result.workflow_state,
            "lifecycle_state": result.lifecycle_state,
        }

    return router


__all__ = [
    "ContainerWorkflowOperations",
    "HostWorkflowOperations",
    "build_container_workflow_router",
    "build_host_workflow_router",
]
