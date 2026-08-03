from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone")
    return value


class OperationKind(str, Enum):
    REBOOT = "reboot"
    WORKLOAD_RESTART = "workload_restart"
    RESOURCE_CHANGE = "resource_change"
    SETTINGS_CHANGE = "settings_change"
    ZONING_CHANGE = "zoning_change"
    APPLY = "apply"
    DETACH = "detach"
    PURGE = "purge"
    DOWNLOAD = "download"
    UPGRADE = "upgrade"


class AvailabilityMode(str, Enum):
    ZERO_IMPACT = "zero-impact"
    AUDITED_OUTAGE = "audited-outage"


class ProviderType(str, Enum):
    NATIVE_PODMAN = "native_podman"
    ADOPTED_PODMAN = "adopted_podman"
    EXTERNAL_API = "external_api"
    ECK_ENDPOINT = "eck_endpoint"


class MaintenanceBackend(str, Enum):
    DOCUMENTED_ROLLING = "documented_rolling"
    NODE_SHUTDOWN_API = "node_shutdown_api"
    NONE = "none"


class SourceStatus(str, Enum):
    OK = "ok"
    STALE = "stale"
    MISSING = "missing"
    ERROR = "error"


class EvaluationStage(str, Enum):
    PLANNING = "planning"
    PREFLIGHT = "preflight"


class PredicateId(str, Enum):
    HOST_ENABLED = "HostEnabled"
    HOST_REACHABLE = "HostReachable"
    NO_CONFLICTING_OPERATION = "NoConflictingOperation"
    MEMBERSHIP_READY = "MembershipReady"
    FRESH_RUNTIME_OBSERVATION = "FreshRuntimeObservation"
    EXPECTED_CLUSTER_IDENTITY = "ExpectedClusterIdentity"
    SUPPORTED_NODE_LIFECYCLE_MODE = "SupportedNodeLifecycleMode"
    CLUSTER_HEALTH = "ClusterHealth"
    NO_SHARD_MOVEMENT = "NoShardMovement"
    NO_LAST_SHARD_COPY = "NoLastShardCopy"
    PRIMARY_PROMOTION_SAFETY = "PrimaryPromotionSafety"
    ALLOCATION_SETTING_CAPTURED = "AllocationSettingCaptured"
    MASTER_QUORUM = "MasterQuorum"
    ROLE_AVAILABILITY_BUDGET = "RoleAvailabilityBudget"
    DISK_WATERMARKS_SAFE = "DiskWatermarksSafe"
    TARGET_ARTIFACT_READY = "TargetArtifactReady"
    VERSION_TRANSITION_SUPPORTED = "VersionTransitionSupported"
    SNAPSHOT_RECOVERY_READY = "SnapshotRecoveryReady"
    NO_STALE_SHUTDOWN_RECORD = "NoStaleShutdownRecord"


class PredicateSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class PredicateOutcome(str, Enum):
    PASSED = "passed"
    WARNING = "warning"
    BLOCKED = "blocked"


class MaintenancePolicy(FrozenModel):
    max_unavailable: int = Field(default=1, ge=1, le=100)
    max_surge: Literal[0] = 0
    minimum_master_eligible: int | Literal["quorum"] = "quorum"
    minimum_data_per_tier: int = Field(default=1, ge=1, le=100)
    minimum_kibana: int = Field(default=1, ge=1, le=100)
    minimum_fleet_server: int = Field(default=1, ge=1, le=100)
    minimum_logstash: int = Field(default=1, ge=1, le=100)
    minimum_coordinating: int = Field(default=1, ge=1, le=100)
    allow_agent_interruption: Literal["true-with-warning", "block"] = "true-with-warning"
    required_cluster_health: Literal["green", "yellow"] = "green"
    allocation_guard: Literal["primaries-for-data", "none"] = "primaries-for-data"
    observation_max_age_seconds: int = Field(default=120, ge=1, le=3600)
    restart_allocation_delay_seconds: int | None = Field(default=None, ge=0, le=86400)
    host_return_timeout_seconds: int = Field(default=900, ge=30, le=86400)
    workload_ready_timeout_seconds: int = Field(default=900, ge=30, le=86400)
    plan_validity_seconds: int = Field(default=300, ge=30, le=3600)

    @field_validator("minimum_master_eligible", mode="before")
    @classmethod
    def validate_minimum_master_eligible(cls, value):
        if value == "quorum":
            return value
        if isinstance(value, bool):
            raise ValueError("minimum_master_eligible must be quorum or a positive integer")
        try:
            parsed = int(value)
        except (TypeError, ValueError) as error:
            raise ValueError("minimum_master_eligible must be quorum or a positive integer") from error
        if parsed < 1 or parsed > 100:
            raise ValueError("minimum_master_eligible must be quorum or a positive integer")
        return parsed


class SourceObservation(FrozenModel):
    source: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    status: SourceStatus
    observed_at: datetime
    required: bool = True
    content_hash: str | None = Field(default=None, max_length=128)
    error_category: str | None = Field(default=None, max_length=128)

    @field_validator("observed_at")
    @classmethod
    def observed_at_is_aware(cls, value):
        return _aware(value, "observed_at")


class HostObservation(FrozenModel):
    node_id: int = Field(ge=1)
    name: str | None = Field(default=None, max_length=128)
    enabled: bool
    initialized: bool
    reachable: bool
    membership_ready: bool
    observed_at: datetime
    boot_id_hash: str | None = Field(default=None, max_length=128)

    @field_validator("observed_at")
    @classmethod
    def observed_at_is_aware(cls, value):
        return _aware(value, "observed_at")


class WorkloadObservation(FrozenModel):
    assignment_id: int = Field(ge=1)
    cluster_id: int = Field(ge=1)
    node_id: int = Field(ge=1)
    name: str | None = Field(default=None, max_length=256)
    role: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    expected_running: bool
    running: bool
    ready: bool
    master_eligible: bool = False
    data_tiers: tuple[str, ...] = ()
    endpoint_required: bool = False
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def observed_at_is_aware(cls, value):
        return _aware(value, "observed_at")

    @field_validator("data_tiers", mode="before")
    @classmethod
    def normalize_tiers(cls, value):
        return tuple(sorted({str(item).strip().lower() for item in (value or ()) if str(item).strip()}))


class ClusterObservation(FrozenModel):
    cluster_id: int = Field(ge=1)
    provider_type: ProviderType
    backend: MaintenanceBackend
    lifecycle_supported: bool
    configured_name: str = Field(min_length=1, max_length=128)
    configured_uuid: str | None = Field(default=None, max_length=128)
    observed_name: str | None = Field(default=None, max_length=128)
    observed_uuid: str | None = Field(default=None, max_length=128)
    health: Literal["green", "yellow", "red", "unknown"]
    master_eligible_total: int = Field(ge=0)
    master_eligible_available: int = Field(ge=0)
    initializing_shards: int = Field(ge=0)
    relocating_shards: int = Field(ge=0)
    no_last_shard_copy: bool
    primary_promotion_safe: bool
    allocation_setting_captured: bool
    disk_watermarks_safe: bool
    target_artifact_ready: bool
    version_transition_supported: bool
    snapshot_recovery_ready: bool
    stale_shutdown_record: bool
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def observed_at_is_aware(cls, value):
        return _aware(value, "observed_at")

    @model_validator(mode="after")
    def available_masters_fit_total(self):
        if self.master_eligible_available > self.master_eligible_total:
            raise ValueError("available master-eligible nodes cannot exceed the total")
        return self

    @property
    def identity_matches(self) -> bool:
        return bool(
            self.observed_name
            and self.observed_name == self.configured_name
            and (self.configured_uuid is None or self.observed_uuid == self.configured_uuid)
        )


class RevisionObservation(FrozenModel):
    assignment_id: int = Field(ge=1)
    revision: int = Field(ge=1)


class PolicyObservation(FrozenModel):
    cluster_id: int = Field(ge=1)
    revision: int = Field(ge=0)
    policy: MaintenancePolicy


class ObservationSnapshot(FrozenModel):
    captured_at: datetime
    capability_revision: str = Field(min_length=1, max_length=128)
    sources: tuple[SourceObservation, ...]
    hosts: tuple[HostObservation, ...]
    clusters: tuple[ClusterObservation, ...]
    workloads: tuple[WorkloadObservation, ...]
    assignment_revisions: tuple[RevisionObservation, ...]
    policies: tuple[PolicyObservation, ...] = ()
    conflicting_operations: tuple[str, ...] = ()

    @field_validator("captured_at")
    @classmethod
    def captured_at_is_aware(cls, value):
        return _aware(value, "captured_at")

    @model_validator(mode="after")
    def normalize_and_validate(self):
        sources = tuple(sorted(self.sources, key=lambda item: item.source))
        hosts = tuple(sorted(self.hosts, key=lambda item: item.node_id))
        clusters = tuple(sorted(self.clusters, key=lambda item: item.cluster_id))
        workloads = tuple(sorted(self.workloads, key=lambda item: item.assignment_id))
        revisions = tuple(sorted(self.assignment_revisions, key=lambda item: item.assignment_id))
        policies = tuple(sorted(self.policies, key=lambda item: item.cluster_id))
        conflicts = tuple(sorted(set(self.conflicting_operations)))
        for values, label, key in (
            (sources, "source", lambda item: item.source),
            (hosts, "host", lambda item: item.node_id),
            (clusters, "cluster", lambda item: item.cluster_id),
            (workloads, "assignment", lambda item: item.assignment_id),
            (revisions, "assignment revision", lambda item: item.assignment_id),
            (policies, "cluster policy", lambda item: item.cluster_id),
        ):
            identifiers = [key(item) for item in values]
            if len(identifiers) != len(set(identifiers)):
                raise ValueError(f"Observation snapshot contains duplicate {label} identifiers")
        object.__setattr__(self, "sources", sources)
        object.__setattr__(self, "hosts", hosts)
        object.__setattr__(self, "clusters", clusters)
        object.__setattr__(self, "workloads", workloads)
        object.__setattr__(self, "assignment_revisions", revisions)
        object.__setattr__(self, "policies", policies)
        object.__setattr__(self, "conflicting_operations", conflicts)
        return self

    def host(self, node_id: int) -> HostObservation | None:
        return next((item for item in self.hosts if item.node_id == node_id), None)

    def cluster(self, cluster_id: int) -> ClusterObservation | None:
        return next((item for item in self.clusters if item.cluster_id == cluster_id), None)


class PlanningTarget(FrozenModel):
    operation: OperationKind
    node_id: int | None = Field(default=None, ge=1)
    cluster_id: int | None = Field(default=None, ge=1)
    assignment_ids: tuple[int, ...] = ()
    reason: str = Field(default="", max_length=512)
    availability_mode: AvailabilityMode = AvailabilityMode.ZERO_IMPACT
    current_version: str | None = Field(default=None, pattern=r"^\d+\.\d+\.\d+$")
    target_version: str | None = Field(default=None, pattern=r"^\d+\.\d+\.\d+$")

    @field_validator("assignment_ids", mode="before")
    @classmethod
    def normalize_assignments(cls, value):
        assignments = tuple(sorted(set(value or ())))
        if any(not isinstance(item, int) or isinstance(item, bool) or item < 1 for item in assignments):
            raise ValueError("Assignment targets must be positive integer identifiers")
        return assignments

    @model_validator(mode="after")
    def validate_scope(self):
        if self.operation == OperationKind.REBOOT and self.node_id is None:
            raise ValueError("A reboot plan requires a node target")
        if self.operation in {OperationKind.SETTINGS_CHANGE, OperationKind.ZONING_CHANGE, OperationKind.DOWNLOAD, OperationKind.UPGRADE} and self.cluster_id is None:
            raise ValueError(f"A {self.operation.value} plan requires a cluster target")
        if self.operation in {OperationKind.WORKLOAD_RESTART, OperationKind.RESOURCE_CHANGE, OperationKind.DETACH, OperationKind.PURGE} and not self.assignment_ids:
            raise ValueError(f"A {self.operation.value} plan requires assignment targets")
        if self.operation == OperationKind.UPGRADE and (not self.current_version or not self.target_version):
            raise ValueError("An upgrade plan requires current and target versions")
        return self

    @property
    def is_major_upgrade(self) -> bool:
        if self.operation != OperationKind.UPGRADE or not self.current_version or not self.target_version:
            return False
        return self.current_version.split(".", 1)[0] != self.target_version.split(".", 1)[0]


class TierAvailability(FrozenModel):
    tier: str
    available_before: int = Field(ge=0)
    available_after: int = Field(ge=0)
    required: int = Field(ge=0)

    @property
    def safe(self) -> bool:
        return self.available_after >= self.required


class RoleAvailability(FrozenModel):
    role: str
    available_before: int = Field(ge=0)
    available_after: int = Field(ge=0)
    required: int = Field(ge=0)

    @property
    def safe(self) -> bool:
        return self.available_after >= self.required


class BudgetViolation(FrozenModel):
    identifier: str = Field(min_length=1, max_length=128)
    summary: str = Field(min_length=1, max_length=512)
    remediation: str = Field(min_length=1, max_length=512)


class ClusterImpact(FrozenModel):
    cluster_id: int = Field(ge=1)
    affected_assignment_ids: tuple[int, ...]
    affected_roles: tuple[str, ...]
    existing_unavailable: int = Field(ge=0)
    planned_unavailable: int = Field(ge=0)
    total_unavailable_after: int = Field(ge=0)
    max_unavailable: int = Field(ge=1)
    master_total: int = Field(ge=0)
    master_available_before: int = Field(ge=0)
    master_available_after: int = Field(ge=0)
    master_required: int = Field(ge=0)
    data_tiers: tuple[TierAvailability, ...]
    services: tuple[RoleAvailability, ...]
    endpoints_lost: tuple[str, ...]
    agent_interruptions: int = Field(ge=0)
    violations: tuple[BudgetViolation, ...]

    @property
    def master_quorum_safe(self) -> bool:
        return self.master_available_after >= self.master_required

    @property
    def violation_ids(self) -> tuple[str, ...]:
        return tuple(item.identifier for item in self.violations)

    def service(self, role: str) -> RoleAvailability:
        item = next((value for value in self.services if value.role == role), None)
        if item is None:
            raise KeyError(role)
        return item


class ImpactManifest(FrozenModel):
    target_node_id: int | None
    affected_cluster_ids: tuple[int, ...]
    affected_assignment_ids: tuple[int, ...]
    clusters: tuple[ClusterImpact, ...]

    @property
    def within_budget(self) -> bool:
        return all(not item.violations for item in self.clusters)

    def cluster(self, cluster_id: int) -> ClusterImpact:
        item = next((value for value in self.clusters if value.cluster_id == cluster_id), None)
        if item is None:
            raise KeyError(cluster_id)
        return item


class PredicateResult(FrozenModel):
    identifier: PredicateId
    severity: PredicateSeverity
    outcome: PredicateOutcome
    applicable: bool
    forceable: bool
    override_applied: bool
    evidence_summary: str = Field(min_length=1, max_length=1024)
    remediation: str = Field(default="", max_length=1024)
    observed_at: datetime

    @field_validator("observed_at")
    @classmethod
    def observed_at_is_aware(cls, value):
        return _aware(value, "observed_at")


class PlanStep(FrozenModel):
    sequence: int = Field(ge=1)
    kind: str = Field(min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    summary: str = Field(min_length=1, max_length=512)
    cluster_id: int | None = Field(default=None, ge=1)
    assignment_id: int | None = Field(default=None, ge=1)
    node_id: int | None = Field(default=None, ge=1)


class RollbackBoundary(FrozenModel):
    before_step: int = Field(ge=1)
    behavior: str = Field(min_length=1, max_length=512)


class CompiledPlan(FrozenModel):
    schema_version: Literal[1] = 1
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)
    target: PlanningTarget
    policy: MaintenancePolicy
    policy_revision: int = Field(ge=0)
    backend: MaintenanceBackend
    observation: ObservationSnapshot
    predicates: tuple[PredicateResult, ...]
    impact: ImpactManifest
    steps: tuple[PlanStep, ...]
    rollback_boundaries: tuple[RollbackBoundary, ...]
    created_at: datetime
    expires_at: datetime
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("created_at", "expires_at")
    @classmethod
    def timestamps_are_aware(cls, value, info):
        return _aware(value, info.field_name)


class ExecutionValidation(FrozenModel):
    valid: bool
    issue_codes: tuple[str, ...]
