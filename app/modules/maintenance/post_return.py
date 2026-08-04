from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Mapping, Protocol

from pydantic import Field, field_validator, model_validator

from app.modules.maintenance.elasticsearch import (
    AllocationCleanupTrigger,
    AllocationGuardCheckpoint,
    AllocationGuardCleanupResult,
)
from app.modules.maintenance.executor import (
    BOOT_ID_PATTERN,
    HostExecutorResult,
    executor_instance_unit,
    validate_cleanup_paths,
    validate_managed_unit,
    validate_operation_id,
)
from app.modules.maintenance.models import FrozenModel


ERROR_PATTERN = r"^[a-z0-9][a-z0-9-]{0,127}$"
REFERENCE_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
VERSION_PATTERN = r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$"
CLUSTER_UUID_PATTERN = r"^[A-Za-z0-9_-]{8,64}$"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone")
    return value


class PostReturnErrorCategory(str, Enum):
    SSH_UNAVAILABLE = "ssh-unavailable"
    BOOT_ID_UNAVAILABLE = "boot-id-unavailable"
    BOOT_ID_UNCHANGED = "boot-id-unchanged"
    BOOT_ID_MISMATCH = "boot-id-mismatch"
    PODMAN_SOCKET_UNAVAILABLE = "podman-socket-unavailable"
    QUADLET_GENERATOR_UNAVAILABLE = "quadlet-generator-unavailable"
    REQUIRED_QUADLET_MISSING = "required-quadlet-missing"
    REQUIRED_WORKLOAD_NOT_RUNNING = "required-workload-not-running"
    ENDPOINT_UNAVAILABLE = "endpoint-unavailable"
    EXECUTOR_RESULT_UNAVAILABLE = "executor-result-unavailable"
    EXECUTOR_RESULT_IDENTITY_MISMATCH = "executor-result-identity-mismatch"
    EXECUTOR_RESULT_RECOVERY_REQUIRED = "executor-result-recovery-required"
    EXECUTOR_RESULT_INCOMPLETE = "executor-result-incomplete"
    NODE_IDENTITY_UNAVAILABLE = "node-identity-unavailable"
    NODE_IDENTITY_MISMATCH = "node-identity-mismatch"
    NODE_VERSION_MISMATCH = "node-version-mismatch"
    CLUSTER_UUID_MISMATCH = "cluster-uuid-mismatch"
    SHARD_RECOVERY_INCOMPLETE = "shard-recovery-incomplete"
    SERVICE_BUDGET_VIOLATION = "service-budget-violation"
    CLUSTER_HEALTH_UNACCEPTABLE = "cluster-health-unacceptable"
    ALLOCATION_RESTORATION_FAILED = "allocation-restoration-failed"
    SHUTDOWN_CLEANUP_FAILED = "shutdown-cleanup-failed"
    EXECUTOR_ARTIFACT_CLEANUP_FAILED = "executor-artifact-cleanup-failed"
    OWNERSHIP_BOUNDARY_REJECTED = "ownership-boundary-rejected"
    LOCK_RELEASE_FAILED = "lock-release-failed"


class CheckStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


class CleanupStatus(str, Enum):
    VERIFIED = "verified"
    ALREADY_CLEAN = "already-clean"
    FAILED = "failed"
    OWNERSHIP_REJECTED = "ownership-rejected"


class PostReturnCheck(FrozenModel):
    check_id: str = Field(pattern=REFERENCE_PATTERN)
    status: CheckStatus
    error_category: PostReturnErrorCategory | None = None

    @model_validator(mode="after")
    def status_matches_error(self):
        if self.status == CheckStatus.FAILED and self.error_category is None:
            raise ValueError("failed checks require an error category")
        if self.status != CheckStatus.FAILED and self.error_category is not None:
            raise ValueError("only failed checks may include an error category")
        return self


class CleanupProof(FrozenModel):
    status: CleanupStatus

    @property
    def proven(self) -> bool:
        return self.status in (CleanupStatus.VERIFIED, CleanupStatus.ALREADY_CLEAN)


class WorkloadExpectation(FrozenModel):
    assignment_id: int = Field(ge=1)
    unit: str

    @field_validator("unit")
    @classmethod
    def unit_is_managed(cls, value):
        return validate_managed_unit(value)


class EndpointExpectation(FrozenModel):
    endpoint_ref: str = Field(pattern=REFERENCE_PATTERN)


class NodeIdentityExpectation(FrozenModel):
    cluster_id: int = Field(ge=1)
    assignment_id: int = Field(ge=1)
    persistent_node_id: str = Field(min_length=1, max_length=256)
    node_name: str = Field(min_length=1, max_length=256)
    version: str = Field(pattern=VERSION_PATTERN)
    cluster_uuid: str = Field(pattern=CLUSTER_UUID_PATTERN)


class NodeIdentityObservation(FrozenModel):
    persistent_node_id: str = Field(min_length=1, max_length=256)
    node_name: str = Field(min_length=1, max_length=256)
    version: str = Field(pattern=VERSION_PATTERN)
    cluster_uuid: str = Field(pattern=CLUSTER_UUID_PATTERN)


class ShardRecoveryObservation(FrozenModel):
    active_recoveries: int = Field(ge=0)
    initializing_shards: int = Field(ge=0)
    relocating_shards: int = Field(ge=0)
    unassigned_primaries: int = Field(ge=0)

    @property
    def complete(self) -> bool:
        return not any(
            (
                self.active_recoveries,
                self.initializing_shards,
                self.relocating_shards,
                self.unassigned_primaries,
            )
        )


class ServiceBudgetExpectation(FrozenModel):
    cluster_id: int = Field(ge=1)
    role: str = Field(pattern=REFERENCE_PATTERN)
    minimum_available: int = Field(ge=0, le=1000)


class ServiceBudgetObservation(FrozenModel):
    available: int = Field(ge=0, le=1000)


class ClusterExpectation(FrozenModel):
    cluster_id: int = Field(ge=1)
    required_health: str = Field(default="green", pattern=r"^(green|yellow)$")
    nodes: tuple[NodeIdentityExpectation, ...] = ()

    @model_validator(mode="after")
    def nodes_belong_to_cluster(self):
        if any(node.cluster_id != self.cluster_id for node in self.nodes):
            raise ValueError("node identity expectations must belong to their cluster")
        assignment_ids = [node.assignment_id for node in self.nodes]
        if len(assignment_ids) != len(set(assignment_ids)):
            raise ValueError("node identity expectations must be unique by assignment")
        return self


class ShutdownCleanupExpectation(FrozenModel):
    cluster_id: int = Field(ge=1)
    persistent_node_id: str = Field(min_length=1, max_length=256)
    node_version: str = Field(pattern=VERSION_PATTERN)


class ExecutorCleanupTarget(FrozenModel):
    operation_id: str
    unit: str
    paths: tuple[str, ...] = ()

    @field_validator("operation_id")
    @classmethod
    def operation_is_safe(cls, value):
        return validate_operation_id(value)

    @model_validator(mode="after")
    def artifacts_are_operation_owned(self):
        if self.unit != executor_instance_unit(self.operation_id):
            raise ValueError("executor cleanup unit is outside the operation ownership boundary")
        validate_cleanup_paths(self.paths, self.operation_id)
        return self


class PostReturnRequest(FrozenModel):
    operation_id: str
    plan_id: str
    node_id: int = Field(ge=1)
    pre_reboot_boot_id: str = Field(pattern=BOOT_ID_PATTERN)
    expected_manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    host_return_timeout_seconds: int = Field(default=900, ge=1, le=86400)
    workloads: tuple[WorkloadExpectation, ...] = ()
    endpoints: tuple[EndpointExpectation, ...] = ()
    clusters: tuple[ClusterExpectation, ...] = ()
    service_budgets: tuple[ServiceBudgetExpectation, ...] = ()
    allocation_guards: tuple[AllocationGuardCheckpoint, ...] = ()
    shutdown_records: tuple[ShutdownCleanupExpectation, ...] = ()
    executor_cleanup: ExecutorCleanupTarget

    @field_validator("operation_id", "plan_id")
    @classmethod
    def identifiers_are_safe(cls, value):
        return validate_operation_id(value)

    @model_validator(mode="after")
    def request_is_consistent(self):
        if self.executor_cleanup.operation_id != self.operation_id:
            raise ValueError("executor cleanup must belong to this operation")
        units = [item.unit for item in self.workloads]
        assignments = [item.assignment_id for item in self.workloads]
        endpoints = [item.endpoint_ref for item in self.endpoints]
        cluster_ids = [item.cluster_id for item in self.clusters]
        if len(units) != len(set(units)) or len(assignments) != len(set(assignments)):
            raise ValueError("previously-running workloads must be unique")
        if units != sorted(units):
            raise ValueError("previously-running workloads must use deterministic unit order")
        if len(endpoints) != len(set(endpoints)):
            raise ValueError("endpoint references must be unique")
        if len(cluster_ids) != len(set(cluster_ids)):
            raise ValueError("cluster expectations must be unique")
        cluster_set = set(cluster_ids)
        if any(item.cluster_id not in cluster_set for item in self.service_budgets):
            raise ValueError("service budgets must reference an affected cluster")
        if any(item.cluster_id not in cluster_set for item in self.allocation_guards):
            raise ValueError("allocation guards must reference an affected cluster")
        if any(item.cluster_id not in cluster_set for item in self.shutdown_records):
            raise ValueError("shutdown cleanup must reference an affected cluster")
        return self


class AllocationCleanupEvidence(FrozenModel):
    cluster_id: int = Field(ge=1)
    status: str = Field(pattern=r"^(restored|recovery_required)$")
    verified: bool
    error_category: str | None = Field(default=None, pattern=ERROR_PATTERN)


class ShutdownCleanupEvidence(FrozenModel):
    cluster_id: int = Field(ge=1)
    persistent_node_id: str = Field(min_length=1, max_length=256)
    status: CleanupStatus


class PostReturnResult(FrozenModel):
    state: str = Field(pattern=r"^(complete|recovery_required)$")
    checks: tuple[PostReturnCheck, ...]
    error_categories: tuple[PostReturnErrorCategory, ...]
    allocation_cleanup: tuple[AllocationCleanupEvidence, ...]
    shutdown_cleanup: tuple[ShutdownCleanupEvidence, ...]
    executor_result_imported: bool
    executor_result_complete: bool
    executor_artifacts_cleaned: bool
    locks_released: bool
    completed_at: datetime

    @field_validator("completed_at")
    @classmethod
    def completed_at_is_aware(cls, value):
        return _aware(value, "completed_at")

    @model_validator(mode="after")
    def completion_is_proven(self):
        if self.state == "complete" and (
            self.error_categories
            or any(check.status != CheckStatus.PASSED for check in self.checks)
            or not self.executor_result_imported
            or not self.executor_result_complete
            or not self.executor_artifacts_cleaned
            or not self.locks_released
            or any(not item.verified or item.status != "restored" for item in self.allocation_cleanup)
            or any(item.status not in (CleanupStatus.VERIFIED, CleanupStatus.ALREADY_CLEAN) for item in self.shutdown_cleanup)
        ):
            raise ValueError("complete post-return results require fully verified cleanup")
        return self


class HostReturnAdapter(Protocol):
    async def wait_for_ssh(self, node_id: int, timeout_seconds: int) -> bool:
        ...

    async def read_boot_id(self, node_id: int) -> str | None:
        ...

    async def podman_socket_ready(self, node_id: int) -> bool:
        ...

    async def quadlet_generator_ready(self, node_id: int) -> bool:
        ...

    async def generated_units(self, node_id: int, units: tuple[str, ...]) -> frozenset[str]:
        ...

    async def unit_states(self, node_id: int, units: tuple[str, ...]) -> Mapping[str, bool]:
        ...

    async def endpoint_ready(self, node_id: int, endpoint_ref: str) -> bool:
        ...


class ClusterReturnAdapter(Protocol):
    async def node_identity(self, expectation: NodeIdentityExpectation) -> NodeIdentityObservation | None:
        ...

    async def shard_recovery(self, cluster_id: int) -> ShardRecoveryObservation:
        ...

    async def service_budget(self, expectation: ServiceBudgetExpectation) -> ServiceBudgetObservation:
        ...

    async def cluster_health(self, cluster_id: int) -> str:
        ...


class AllocationRestorer(Protocol):
    async def restore(
        self,
        checkpoint: AllocationGuardCheckpoint,
        *,
        trigger: AllocationCleanupTrigger,
    ) -> AllocationGuardCleanupResult:
        ...


class ShutdownCleaner(Protocol):
    async def clear_shutdown(self, expectation: ShutdownCleanupExpectation) -> CleanupProof:
        ...


class ExecutorResultImporter(Protocol):
    async def import_result(self, operation_id: str) -> HostExecutorResult:
        ...


class ManagedArtifactCleaner(Protocol):
    async def cleanup_executor(self, target: ExecutorCleanupTarget) -> CleanupProof:
        ...


class MaintenanceLockReleaser(Protocol):
    async def release(
        self,
        *,
        plan_id: str,
        node_id: int,
        cluster_ids: tuple[int, ...],
    ) -> CleanupProof:
        ...


class PostReturnCoordinator:
    """Verify a returned host and finalize only after cleanup is proven.

    All I/O is injected. The coordinator never opens a socket, runs a command, or
    removes a path itself. Adapter exceptions are deliberately reduced to stable,
    redacted categories.
    """

    def __init__(
        self,
        *,
        host: HostReturnAdapter,
        cluster: ClusterReturnAdapter,
        allocation: AllocationRestorer,
        shutdown: ShutdownCleaner,
        executor_results: ExecutorResultImporter,
        artifacts: ManagedArtifactCleaner,
        locks: MaintenanceLockReleaser,
        clock=_utc_now,
    ):
        self.host = host
        self.cluster = cluster
        self.allocation = allocation
        self.shutdown = shutdown
        self.executor_results = executor_results
        self.artifacts = artifacts
        self.locks = locks
        self.clock = clock

    async def verify_and_cleanup(self, request: PostReturnRequest) -> PostReturnResult:
        checks: list[PostReturnCheck] = []
        errors: list[PostReturnErrorCategory] = []
        allocation_evidence: list[AllocationCleanupEvidence] = []
        shutdown_evidence: list[ShutdownCleanupEvidence] = []
        executor_imported = False
        executor_complete = False
        executor_cleaned = False
        locks_released = False

        def pass_check(check_id: str) -> None:
            checks.append(PostReturnCheck(check_id=check_id, status=CheckStatus.PASSED))

        def fail_check(check_id: str, category: PostReturnErrorCategory) -> None:
            checks.append(
                PostReturnCheck(
                    check_id=check_id,
                    status=CheckStatus.FAILED,
                    error_category=category,
                )
            )
            if category not in errors:
                errors.append(category)

        def skip_check(check_id: str) -> None:
            checks.append(PostReturnCheck(check_id=check_id, status=CheckStatus.SKIPPED))

        ssh_ready = await self._boolean_call(
            self.host.wait_for_ssh(request.node_id, request.host_return_timeout_seconds)
        )
        if ssh_ready:
            pass_check("ssh-return")
        else:
            fail_check("ssh-return", PostReturnErrorCategory.SSH_UNAVAILABLE)

        observed_boot_id: str | None = None
        if ssh_ready:
            try:
                observed_boot_id = await self.host.read_boot_id(request.node_id)
            except Exception:
                observed_boot_id = None
            if observed_boot_id is None:
                fail_check("boot-id", PostReturnErrorCategory.BOOT_ID_UNAVAILABLE)
            elif observed_boot_id == request.pre_reboot_boot_id:
                fail_check("boot-id", PostReturnErrorCategory.BOOT_ID_UNCHANGED)
            else:
                pass_check("boot-id")
        else:
            skip_check("boot-id")

        if ssh_ready:
            if await self._boolean_call(self.host.podman_socket_ready(request.node_id)):
                pass_check("podman-socket")
            else:
                fail_check("podman-socket", PostReturnErrorCategory.PODMAN_SOCKET_UNAVAILABLE)
            quadlet_ready = await self._boolean_call(self.host.quadlet_generator_ready(request.node_id))
            if quadlet_ready:
                pass_check("quadlet-generator")
            else:
                fail_check("quadlet-generator", PostReturnErrorCategory.QUADLET_GENERATOR_UNAVAILABLE)
            await self._verify_workloads(request, checks, errors, pass_check, fail_check, skip_check, quadlet_ready)
            await self._verify_endpoints(request, pass_check, fail_check)
        else:
            for check_id in ("podman-socket", "quadlet-generator", "required-quadlets", "required-workloads"):
                skip_check(check_id)
            for endpoint in request.endpoints:
                skip_check(f"endpoint:{endpoint.endpoint_ref}")

        for cluster in request.clusters:
            await self._verify_cluster(cluster, pass_check, fail_check)
        for budget in request.service_budgets:
            await self._verify_budget(budget, pass_check, fail_check)

        if ssh_ready:
            executor_imported, executor_complete = await self._verify_executor_result(
                request,
                observed_boot_id,
                pass_check,
                fail_check,
            )
        else:
            skip_check("executor-result")

        for checkpoint in request.allocation_guards:
            try:
                cleanup = await self.allocation.restore(
                    checkpoint,
                    trigger=AllocationCleanupTrigger.RECOVERY,
                )
            except Exception:
                cleanup = None
            if cleanup is not None:
                allocation_evidence.append(
                    AllocationCleanupEvidence(
                        cluster_id=checkpoint.cluster_id,
                        status=cleanup.status,
                        verified=cleanup.verified,
                        error_category=cleanup.error_category,
                    )
                )
            if cleanup is not None and cleanup.status == "restored" and cleanup.verified:
                pass_check(f"allocation-cleanup:{checkpoint.cluster_id}")
            else:
                fail_check(
                    f"allocation-cleanup:{checkpoint.cluster_id}",
                    PostReturnErrorCategory.ALLOCATION_RESTORATION_FAILED,
                )

        for shutdown_record in request.shutdown_records:
            try:
                proof = await self.shutdown.clear_shutdown(shutdown_record)
            except Exception:
                proof = CleanupProof(status=CleanupStatus.FAILED)
            shutdown_evidence.append(
                ShutdownCleanupEvidence(
                    cluster_id=shutdown_record.cluster_id,
                    persistent_node_id=shutdown_record.persistent_node_id,
                    status=proof.status,
                )
            )
            if proof.proven:
                pass_check(f"shutdown-cleanup:{shutdown_record.cluster_id}")
            else:
                fail_check(
                    f"shutdown-cleanup:{shutdown_record.cluster_id}",
                    PostReturnErrorCategory.SHUTDOWN_CLEANUP_FAILED,
                )

        verification_proven = not errors and executor_complete
        if verification_proven:
            try:
                artifact_proof = await self.artifacts.cleanup_executor(request.executor_cleanup)
            except Exception:
                artifact_proof = CleanupProof(status=CleanupStatus.FAILED)
            if artifact_proof.proven:
                executor_cleaned = True
                pass_check("executor-artifact-cleanup")
            elif artifact_proof.status == CleanupStatus.OWNERSHIP_REJECTED:
                fail_check(
                    "executor-artifact-cleanup",
                    PostReturnErrorCategory.OWNERSHIP_BOUNDARY_REJECTED,
                )
            else:
                fail_check(
                    "executor-artifact-cleanup",
                    PostReturnErrorCategory.EXECUTOR_ARTIFACT_CLEANUP_FAILED,
                )
        else:
            skip_check("executor-artifact-cleanup")

        if executor_cleaned and not errors:
            try:
                lock_proof = await self.locks.release(
                    plan_id=request.plan_id,
                    node_id=request.node_id,
                    cluster_ids=tuple(cluster.cluster_id for cluster in request.clusters),
                )
            except Exception:
                lock_proof = CleanupProof(status=CleanupStatus.FAILED)
            if lock_proof.proven:
                locks_released = True
                pass_check("lock-release")
            else:
                fail_check("lock-release", PostReturnErrorCategory.LOCK_RELEASE_FAILED)
        else:
            skip_check("lock-release")

        return PostReturnResult(
            state="complete" if not errors and locks_released else "recovery_required",
            checks=tuple(checks),
            error_categories=tuple(errors),
            allocation_cleanup=tuple(allocation_evidence),
            shutdown_cleanup=tuple(shutdown_evidence),
            executor_result_imported=executor_imported,
            executor_result_complete=executor_complete,
            executor_artifacts_cleaned=executor_cleaned,
            locks_released=locks_released,
            completed_at=self.clock(),
        )

    @staticmethod
    async def _boolean_call(awaitable) -> bool:
        try:
            return (await awaitable) is True
        except Exception:
            return False

    async def _verify_workloads(
        self,
        request,
        _checks,
        _errors,
        pass_check,
        fail_check,
        skip_check,
        quadlet_ready,
    ) -> None:
        required = tuple(item.unit for item in request.workloads)
        if not quadlet_ready:
            skip_check("required-quadlets")
            skip_check("required-workloads")
            return
        try:
            generated = await self.host.generated_units(request.node_id, required)
        except Exception:
            generated = frozenset()
        if set(required).issubset(generated):
            pass_check("required-quadlets")
        else:
            fail_check("required-quadlets", PostReturnErrorCategory.REQUIRED_QUADLET_MISSING)
        try:
            states = await self.host.unit_states(request.node_id, required)
        except Exception:
            states = {}
        if set(states) == set(required) and all(states.get(unit) is True for unit in required):
            pass_check("required-workloads")
        else:
            fail_check(
                "required-workloads",
                PostReturnErrorCategory.REQUIRED_WORKLOAD_NOT_RUNNING,
            )

    async def _verify_endpoints(self, request, pass_check, fail_check) -> None:
        for endpoint in request.endpoints:
            check_id = f"endpoint:{endpoint.endpoint_ref}"
            if await self._boolean_call(self.host.endpoint_ready(request.node_id, endpoint.endpoint_ref)):
                pass_check(check_id)
            else:
                fail_check(check_id, PostReturnErrorCategory.ENDPOINT_UNAVAILABLE)

    async def _verify_cluster(self, expectation, pass_check, fail_check) -> None:
        for node in expectation.nodes:
            check_id = f"node-identity:{node.assignment_id}"
            try:
                observed = await self.cluster.node_identity(node)
            except Exception:
                observed = None
            if observed is None:
                fail_check(check_id, PostReturnErrorCategory.NODE_IDENTITY_UNAVAILABLE)
            elif observed.persistent_node_id != node.persistent_node_id or observed.node_name != node.node_name:
                fail_check(check_id, PostReturnErrorCategory.NODE_IDENTITY_MISMATCH)
            elif observed.version != node.version:
                fail_check(check_id, PostReturnErrorCategory.NODE_VERSION_MISMATCH)
            elif observed.cluster_uuid != node.cluster_uuid:
                fail_check(check_id, PostReturnErrorCategory.CLUSTER_UUID_MISMATCH)
            else:
                pass_check(check_id)
        try:
            recovery = await self.cluster.shard_recovery(expectation.cluster_id)
        except Exception:
            recovery = None
        if recovery is not None and recovery.complete:
            pass_check(f"shard-recovery:{expectation.cluster_id}")
        else:
            fail_check(
                f"shard-recovery:{expectation.cluster_id}",
                PostReturnErrorCategory.SHARD_RECOVERY_INCOMPLETE,
            )
        try:
            health = await self.cluster.cluster_health(expectation.cluster_id)
        except Exception:
            health = "unknown"
        ranks = {"red": 0, "yellow": 1, "green": 2}
        if health in ranks and ranks[health] >= ranks[expectation.required_health]:
            pass_check(f"cluster-health:{expectation.cluster_id}")
        else:
            fail_check(
                f"cluster-health:{expectation.cluster_id}",
                PostReturnErrorCategory.CLUSTER_HEALTH_UNACCEPTABLE,
            )

    async def _verify_budget(self, expectation, pass_check, fail_check) -> None:
        check_id = f"service-budget:{expectation.cluster_id}:{expectation.role}"
        try:
            observation = await self.cluster.service_budget(expectation)
        except Exception:
            observation = None
        if observation is not None and observation.available >= expectation.minimum_available:
            pass_check(check_id)
        else:
            fail_check(check_id, PostReturnErrorCategory.SERVICE_BUDGET_VIOLATION)

    async def _verify_executor_result(
        self,
        request,
        observed_boot_id,
        pass_check,
        fail_check,
    ) -> tuple[bool, bool]:
        try:
            result = await self.executor_results.import_result(request.operation_id)
        except Exception:
            fail_check("executor-result", PostReturnErrorCategory.EXECUTOR_RESULT_UNAVAILABLE)
            return False, False
        if (
            result.operation_id != request.operation_id
            or result.plan_id != request.plan_id
            or result.manifest_hash != request.expected_manifest_hash
            or result.pre_reboot_boot_id != request.pre_reboot_boot_id
        ):
            fail_check("executor-result", PostReturnErrorCategory.EXECUTOR_RESULT_IDENTITY_MISMATCH)
            return True, False
        if result.observed_boot_id != observed_boot_id:
            fail_check("executor-result", PostReturnErrorCategory.BOOT_ID_MISMATCH)
            return True, False
        if result.state == "recovery_required":
            fail_check("executor-result", PostReturnErrorCategory.EXECUTOR_RESULT_RECOVERY_REQUIRED)
            return True, False
        expected_units = {item.unit for item in request.workloads}
        observed_units = {item.unit for item in result.units if item.active}
        if observed_units != expected_units:
            fail_check("executor-result", PostReturnErrorCategory.EXECUTOR_RESULT_INCOMPLETE)
            return True, False
        pass_check("executor-result")
        return True, True
