"""Public contracts for checkpointed workload maintenance.

This module deliberately contains no remote execution.  It gives the maintenance
engine and workload module a shared, typed boundary for readiness, disruption
budgets, and rollback decisions before any role-specific adapter is enabled.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class WorkloadOperation(str, Enum):
    RESTART = "restart"
    RESOURCE_CHANGE = "resource_change"
    CERTIFICATE_ROTATION = "certificate_rotation"
    CONFIGURATION_CHANGE = "configuration_change"


class WorkloadRole(str, Enum):
    ELASTICSEARCH = "elasticsearch"
    KIBANA = "kibana"
    FLEET_SERVER = "fleet_server"
    LOGSTASH = "logstash"
    ELASTIC_AGENT = "elastic_agent"
    FILEBEAT = "filebeat"
    METRICBEAT = "metricbeat"


class ReadinessEvidence(BaseModel):
    """Evidence captured after a single workload side effect."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ready: bool
    observed_at: str = Field(min_length=1, max_length=64)
    identity_matches: bool = True
    version: str | None = Field(default=None, max_length=32)
    image_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    detail: str = Field(default="", max_length=512)


class DisruptionBudget(BaseModel):
    """Role budget evaluated without changing workload state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    available_before: int = Field(ge=0)
    existing_unavailable: int = Field(default=0, ge=0)
    planned_unavailable: int = Field(default=1, ge=1)
    minimum_ready: int = Field(default=0, ge=0)
    max_unavailable: int = Field(default=1, ge=1)

    @property
    def available_after(self) -> int:
        return max(0, self.available_before - self.planned_unavailable)

    @property
    def total_unavailable_after(self) -> int:
        return self.existing_unavailable + self.planned_unavailable

    @property
    def safe(self) -> bool:
        return (
            self.total_unavailable_after <= self.max_unavailable
            and self.available_after >= self.minimum_ready
        )

    @property
    def reason(self) -> str:
        if self.total_unavailable_after > self.max_unavailable:
            return "disruption_budget_exceeded"
        if self.available_after < self.minimum_ready:
            return "minimum_ready_budget_not_met"
        return "within_budget"


class WorkloadMaintenanceTarget(BaseModel):
    """Immutable target identity used by one-workload checkpoints."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    assignment_id: int = Field(ge=1)
    cluster_id: int = Field(ge=1)
    node_id: int = Field(ge=1)
    role: WorkloadRole
    operation: WorkloadOperation
    expected_name: str = Field(min_length=1, max_length=256)
    expected_image: str = Field(min_length=1, max_length=512)
    expected_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    budget: DisruptionBudget


class WorkloadMaintenancePlanInput(BaseModel):
    """Public input for one fail-closed workload maintenance plan.

    Planning is intentionally separate from execution.  The maintenance module
    persists this immutable target and its rollback boundary, while a future
    approved orchestration adapter may consume it one workload at a time.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    target: WorkloadMaintenanceTarget
    reason: str = Field(min_length=1, max_length=512)
    idempotency_key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")

    @model_validator(mode="after")
    def normalize_reason(self) -> "WorkloadMaintenancePlanInput":
        if not self.reason.strip():
            raise ValueError("reason must not be blank")
        return self


class LegacyWorkloadObservation(BaseModel):
    """Redacted runtime observation for legacy batch-recovery classification."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    assignment_id: int = Field(ge=1)
    role: WorkloadRole
    process_started: bool
    identity_matches: bool
    ready: bool


class LegacyBatchRecoveryDecision(BaseModel):
    """Observation-driven decision supplied to the legacy workload worker.

    The legacy worker never interprets a persisted completed-list as evidence
    that a remote change completed.  It may roll back only the explicitly
    listed stateless assignments.  Any uncertain or Elasticsearch-new-process
    state is handed to an operator as ``recovery_required``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    classification: Literal["rollback", "recovery_required", "no_observation"]
    reason: str = Field(min_length=1, max_length=256)
    rollback_assignment_ids: tuple[int, ...] = ()

    @model_validator(mode="after")
    def validate_rollback_scope(self) -> "LegacyBatchRecoveryDecision":
        values = tuple(sorted(set(self.rollback_assignment_ids)))
        if any(item < 1 for item in values):
            raise ValueError("rollback_assignment_ids must contain positive identifiers")
        if self.classification != "rollback" and values:
            raise ValueError("only rollback decisions may list rollback assignments")
        object.__setattr__(self, "rollback_assignment_ids", values)
        return self


class WorkloadCheckpoint(BaseModel):
    """Persistable checkpoint for exactly one workload side effect."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sequence: int = Field(ge=1)
    target: WorkloadMaintenanceTarget
    state: Literal["pending", "prepared", "started", "ready", "failed", "recovery_required"] = "pending"
    before_image: str | None = Field(default=None, max_length=512)
    before_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    after: ReadinessEvidence | None = None

    @model_validator(mode="after")
    def validate_ready_state(self) -> "WorkloadCheckpoint":
        if self.state == "ready" and (self.after is None or not self.after.ready):
            raise ValueError("A ready checkpoint requires successful readiness evidence")
        if self.state == "recovery_required" and self.after is not None and self.after.ready:
            raise ValueError("A recovery-required checkpoint cannot contain ready evidence")
        return self


ROLE_MAINTENANCE_PROFILES: dict[WorkloadRole, dict[str, object]] = {
    WorkloadRole.ELASTICSEARCH: {
        "requires_cluster_identity": True,
        "requires_peer_health": True,
        "stateless_rollback": False,
    },
    WorkloadRole.KIBANA: {"requires_cluster_identity": True, "requires_peer_health": False, "stateless_rollback": True},
    WorkloadRole.FLEET_SERVER: {"requires_cluster_identity": True, "requires_peer_health": False, "stateless_rollback": True},
    WorkloadRole.LOGSTASH: {"requires_cluster_identity": False, "requires_peer_health": False, "stateless_rollback": True},
    WorkloadRole.ELASTIC_AGENT: {"requires_cluster_identity": False, "requires_peer_health": False, "stateless_rollback": True},
    WorkloadRole.FILEBEAT: {"requires_cluster_identity": False, "requires_peer_health": False, "stateless_rollback": True},
    WorkloadRole.METRICBEAT: {"requires_cluster_identity": False, "requires_peer_health": False, "stateless_rollback": True},
}


def validate_readiness(target: WorkloadMaintenanceTarget, evidence: ReadinessEvidence) -> tuple[bool, str]:
    """Validate post-side-effect identity and role-specific readiness."""

    if not evidence.ready:
        return False, "workload_not_ready"
    if not evidence.identity_matches:
        return False, "workload_identity_mismatch"
    profile = ROLE_MAINTENANCE_PROFILES[target.role]
    if target.expected_digest and evidence.image_digest != target.expected_digest:
        return False, "workload_digest_mismatch"
    if target.expected_image and evidence.version and target.expected_image.endswith(evidence.version) is False:
        # Version is optional for legacy probes; when present it must agree with
        # the immutable target image tag.
        return False, "workload_version_mismatch"
    if profile["requires_cluster_identity"] and not evidence.identity_matches:
        return False, "cluster_identity_required"
    return True, "ready"


def rollback_allowed(role: WorkloadRole | str, *, process_started: bool) -> bool:
    """Stateless artifacts may roll back; Elasticsearch never auto-downgrades."""

    normalized = WorkloadRole(role)
    return not process_started or bool(ROLE_MAINTENANCE_PROFILES[normalized]["stateless_rollback"])


def legacy_role_to_workload_role(role: str) -> WorkloadRole:
    """Normalize controller role names before applying rollback policy."""

    normalized = role.strip().lower()
    if normalized in {"bootstrap_master", "master", "hot", "warm", "ml", "ingest", "elasticsearch"}:
        return WorkloadRole.ELASTICSEARCH
    aliases = {
        "fleet": WorkloadRole.FLEET_SERVER,
        "fleet_server": WorkloadRole.FLEET_SERVER,
        "fleet-server": WorkloadRole.FLEET_SERVER,
        "agent": WorkloadRole.ELASTIC_AGENT,
        "elastic_agent": WorkloadRole.ELASTIC_AGENT,
        "elastic-agent": WorkloadRole.ELASTIC_AGENT,
    }
    if normalized in aliases:
        return aliases[normalized]
    return WorkloadRole(normalized)


def classify_legacy_batch_recovery(
    changes: list[dict],
    observations: tuple[LegacyWorkloadObservation, ...],
) -> LegacyBatchRecoveryDecision:
    """Classify interrupted batch work from runtime observations, not progress.

    Missing observations are intentionally not treated as permission to roll
    back.  A possibly started Elasticsearch process is never automatically
    reverted because it may have opened its persistent data path.
    """

    targets = {
        int(change["assignment_id"]): legacy_role_to_workload_role(str(change["role"]))
        for change in changes
        if change.get("assignment_id") is not None and change.get("role") is not None
    }
    if not observations:
        return LegacyBatchRecoveryDecision(classification="no_observation", reason="runtime_observation_unavailable")
    observed = {item.assignment_id: item for item in observations if item.assignment_id in targets}
    missing = sorted(set(targets) - set(observed))
    if missing:
        return LegacyBatchRecoveryDecision(
            classification="recovery_required",
            reason="runtime_observation_missing",
        )
    blocked = [
        item.assignment_id
        for item in observed.values()
        if item.process_started and not rollback_allowed(item.role, process_started=True)
    ]
    if blocked:
        return LegacyBatchRecoveryDecision(
            classification="recovery_required",
            reason="elasticsearch_process_may_have_opened_data",
        )
    uncertain = [item.assignment_id for item in observed.values() if not item.identity_matches]
    if uncertain:
        return LegacyBatchRecoveryDecision(
            classification="recovery_required",
            reason="workload_identity_unconfirmed",
        )
    return LegacyBatchRecoveryDecision(
        classification="rollback",
        reason="stateless_workloads_observed",
        rollback_assignment_ids=tuple(sorted(observed)),
    )


__all__ = [
    "DisruptionBudget",
    "LegacyBatchRecoveryDecision",
    "LegacyWorkloadObservation",
    "ReadinessEvidence",
    "ROLE_MAINTENANCE_PROFILES",
    "WorkloadCheckpoint",
    "WorkloadMaintenancePlanInput",
    "WorkloadMaintenanceTarget",
    "WorkloadOperation",
    "WorkloadRole",
    "classify_legacy_batch_recovery",
    "legacy_role_to_workload_role",
    "rollback_allowed",
    "validate_readiness",
]
