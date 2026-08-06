"""Public DTOs for planned host and managed-container interruption workflows."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import Field, field_validator, model_validator

from .models import FrozenModel


class MaintenanceTargetScope(str, Enum):
    """The explicit operator-facing target for a temporary interruption."""

    HOST = "host"
    CONTAINER = "container"


class MaintenanceWorkflowState(str, Enum):
    """Stable workflow state independent from legacy persisted plan states."""

    AVAILABLE = "available"
    PREPARING = "preparing"
    READY_TO_STOP = "ready_to_stop"
    STOPPING = "stopping"
    MAINTENANCE = "maintenance"
    RETURNING = "returning"
    VERIFYING = "verifying"
    BLOCKED = "blocked"
    RECOVERY_REQUIRED = "recovery_required"


class MaintenanceWorkflowAction(str, Enum):
    PREPARE = "prepare"
    STOP = "stop"
    RETURN = "return"
    RECOVER = "recover"


class HostMaintenanceTarget(FrozenModel):
    scope: Literal[MaintenanceTargetScope.HOST] = MaintenanceTargetScope.HOST
    node_id: int = Field(ge=1)


class ContainerMaintenanceTarget(FrozenModel):
    scope: Literal[MaintenanceTargetScope.CONTAINER] = MaintenanceTargetScope.CONTAINER
    assignment_id: int = Field(ge=1)


MaintenanceTarget = Annotated[
    Union[HostMaintenanceTarget, ContainerMaintenanceTarget],
    Field(discriminator="scope"),
]


class MaintenanceAffectedWorkload(FrozenModel):
    assignment_id: int = Field(ge=1)
    cluster_id: int = Field(ge=1)
    node_id: int = Field(ge=1)
    role: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    name: str = Field(min_length=1, max_length=256)


class MaintenanceAffectedCluster(FrozenModel):
    cluster_id: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=128)
    data_node_affected: bool = False


class MaintenancePreflightEvidence(FrozenModel):
    identifier: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z][A-Za-z0-9._-]*$")
    outcome: Literal["passed", "warning", "blocked"]
    summary: str = Field(min_length=1, max_length=1024)
    remediation: str = Field(default="", max_length=1024)
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def observed_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observed_at must include a timezone")
        return value


class AllocationGuardStatus(FrozenModel):
    """Redacted allocation-guard ownership projected for operators."""

    cluster_id: int = Field(ge=1)
    owner_plan_id: str = Field(min_length=1, max_length=128)
    phase: Literal["captured", "active", "restored", "recovery_required"]
    setting: Literal["cluster.routing.allocation.enable"] = "cluster.routing.allocation.enable"
    captured_persistent: str | None = Field(default=None, max_length=128)
    captured_transient: str | None = Field(default=None, max_length=128)
    observed_effective: str | None = Field(default=None, max_length=128)
    updated_at: datetime

    @field_validator("updated_at")
    @classmethod
    def updated_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("updated_at must include a timezone")
        return value


class MaintenanceWorkflowCheckpoint(FrozenModel):
    sequence: int = Field(ge=1)
    key: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    state: Literal["pending", "active", "verified", "failed", "recovery_required"]
    summary: str = Field(min_length=1, max_length=512)
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def observed_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observed_at must include a timezone")
        return value


class MaintenanceActionAvailability(FrozenModel):
    action: MaintenanceWorkflowAction
    enabled: bool
    reason: str = Field(default="", max_length=512)

    @model_validator(mode="after")
    def disabled_action_has_reason(self) -> "MaintenanceActionAvailability":
        if not self.enabled and not self.reason.strip():
            raise ValueError("A disabled maintenance action requires a reason")
        return self


class MaintenanceWorkflowSummary(FrozenModel):
    """Read model shared by maintenance, dashboard, host, and workload views."""

    state: MaintenanceWorkflowState
    target: MaintenanceTarget
    affected_workloads: tuple[MaintenanceAffectedWorkload, ...] = ()
    affected_clusters: tuple[MaintenanceAffectedCluster, ...] = ()
    preflight: tuple[MaintenancePreflightEvidence, ...] = ()
    allocation_guards: tuple[AllocationGuardStatus, ...] = ()
    checkpoints: tuple[MaintenanceWorkflowCheckpoint, ...] = ()
    actions: tuple[MaintenanceActionAvailability, ...] = ()

    @model_validator(mode="after")
    def identifiers_are_unique(self) -> "MaintenanceWorkflowSummary":
        values = (
            (self.affected_workloads, "assignment_id", "affected workload"),
            (self.affected_clusters, "cluster_id", "affected cluster"),
            (self.allocation_guards, "cluster_id", "allocation guard"),
            (self.checkpoints, "sequence", "checkpoint sequence"),
            (self.actions, "action", "maintenance action"),
        )
        for items, attribute, label in values:
            identifiers = [getattr(item, attribute) for item in items]
            if len(identifiers) != len(set(identifiers)):
                raise ValueError(f"Maintenance workflow contains duplicate {label} identifiers")
        return self
