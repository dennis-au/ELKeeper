from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from pydantic import ValidationError

from app.maintenance_elasticsearch import (
    AllocationCleanupTrigger,
    AllocationGuardCheckpoint,
    AllocationGuardCleanupResult,
    AllocationGuardPhase,
    capture_allocation_setting,
)
from app.maintenance_executor import (
    ExecutorCheckResult,
    ExecutorUnitResult,
    HostExecutorResult,
    executor_instance_unit,
    executor_paths,
)
from app.maintenance_post_return import (
    CleanupProof,
    CleanupStatus,
    ClusterExpectation,
    EndpointExpectation,
    ExecutorCleanupTarget,
    NodeIdentityExpectation,
    NodeIdentityObservation,
    PostReturnCoordinator,
    PostReturnErrorCategory,
    PostReturnRequest,
    ServiceBudgetExpectation,
    ServiceBudgetObservation,
    ShardRecoveryObservation,
    ShutdownCleanupExpectation,
    WorkloadExpectation,
)


NOW = datetime(2026, 8, 3, 0, 0, tzinfo=timezone.utc)
OPERATION_ID = "0123456789abcdef0123456789abcdef"
PLAN_ID = "fedcba9876543210fedcba9876543210"
PRE_BOOT_ID = "01234567-89ab-cdef-0123-456789abcdef"
POST_BOOT_ID = "11111111-2222-3333-4444-555555555555"
MANIFEST_HASH = "a" * 64
UNIT = "ecp-alpha-hot-1.service"
CLUSTER_UUID = "cluster_uuid_alpha"


def allocation_checkpoint(phase=AllocationGuardPhase.ACTIVE):
    capture = capture_allocation_setting(
        {"persistent": {}, "transient": {}},
        captured_at=NOW,
    )
    return AllocationGuardCheckpoint(
        plan_id=PLAN_ID,
        cluster_id=7,
        phase=phase,
        captured=capture,
        observed=capture,
        updated_at=NOW,
    )


def allocation_result(*, restored=True):
    checkpoint = allocation_checkpoint(
        AllocationGuardPhase.RESTORED if restored else AllocationGuardPhase.RECOVERY_REQUIRED
    )
    return AllocationGuardCleanupResult(
        status="restored" if restored else "recovery_required",
        trigger=AllocationCleanupTrigger.RECOVERY,
        verified=restored,
        checkpoint=checkpoint,
        error_category=None if restored else "allocation-restoration-verification-failed",
    )


def executor_result(*, recovery_required=False):
    return HostExecutorResult(
        operation_id=OPERATION_ID,
        plan_id=PLAN_ID,
        manifest_hash=MANIFEST_HASH,
        state="recovery_required" if recovery_required else "complete",
        reason_code="check_failed" if recovery_required else "completed",
        pre_reboot_boot_id=PRE_BOOT_ID,
        observed_boot_id=POST_BOOT_ID,
        units=(ExecutorUnitResult(unit=UNIT, active=not recovery_required),),
        checks=(
            ExecutorCheckResult(
                check_id="unit-return",
                passed=not recovery_required,
                error_category="unit_timeout" if recovery_required else None,
            ),
        ),
        started_at=NOW,
        completed_at=NOW + timedelta(minutes=1),
    )


def cleanup_target(paths=None):
    owned = executor_paths(OPERATION_ID)
    return ExecutorCleanupTarget(
        operation_id=OPERATION_ID,
        unit=executor_instance_unit(OPERATION_ID),
        paths=paths
        if paths is not None
        else (
            str(owned.manifest),
            str(owned.public_key),
            str(owned.checkpoint),
            str(owned.result),
        ),
    )


def request():
    return PostReturnRequest(
        operation_id=OPERATION_ID,
        plan_id=PLAN_ID,
        node_id=4,
        pre_reboot_boot_id=PRE_BOOT_ID,
        expected_manifest_hash=MANIFEST_HASH,
        workloads=(WorkloadExpectation(assignment_id=11, unit=UNIT),),
        endpoints=(EndpointExpectation(endpoint_ref="elasticsearch-http"),),
        clusters=(
            ClusterExpectation(
                cluster_id=7,
                required_health="yellow",
                nodes=(
                    NodeIdentityExpectation(
                        cluster_id=7,
                        assignment_id=11,
                        persistent_node_id="persistent-node-1",
                        node_name="alpha-hot-1",
                        version="9.1.0",
                        cluster_uuid=CLUSTER_UUID,
                    ),
                ),
            ),
        ),
        service_budgets=(
            ServiceBudgetExpectation(cluster_id=7, role="hot", minimum_available=2),
        ),
        allocation_guards=(allocation_checkpoint(),),
        shutdown_records=(
            ShutdownCleanupExpectation(
                cluster_id=7,
                persistent_node_id="persistent-node-1",
                node_version="9.1.0",
            ),
        ),
        executor_cleanup=cleanup_target(),
    )


class HostFake:
    def __init__(self):
        self.ssh = True
        self.boot_id = POST_BOOT_ID
        self.podman = True
        self.quadlet = True
        self.generated = frozenset({UNIT})
        self.states = {UNIT: True}
        self.endpoint = True

    async def wait_for_ssh(self, node_id, timeout_seconds):
        return self.ssh

    async def read_boot_id(self, node_id):
        return self.boot_id

    async def podman_socket_ready(self, node_id):
        return self.podman

    async def quadlet_generator_ready(self, node_id):
        return self.quadlet

    async def generated_units(self, node_id, units):
        return self.generated

    async def unit_states(self, node_id, units):
        return self.states

    async def endpoint_ready(self, node_id, endpoint_ref):
        return self.endpoint


class ClusterFake:
    def __init__(self):
        self.identity = NodeIdentityObservation(
            persistent_node_id="persistent-node-1",
            node_name="alpha-hot-1",
            version="9.1.0",
            cluster_uuid=CLUSTER_UUID,
        )
        self.recovery = ShardRecoveryObservation(
            active_recoveries=0,
            initializing_shards=0,
            relocating_shards=0,
            unassigned_primaries=0,
        )
        self.available = 2
        self.health = "green"

    async def node_identity(self, expectation):
        return self.identity

    async def shard_recovery(self, cluster_id):
        return self.recovery

    async def service_budget(self, expectation):
        return ServiceBudgetObservation(available=self.available)

    async def cluster_health(self, cluster_id):
        return self.health


class AllocationFake:
    def __init__(self, events, result=None):
        self.events = events
        self.result = result or allocation_result()

    async def restore(self, checkpoint, *, trigger):
        self.events.append("allocation")
        self.trigger = trigger
        return self.result


class ShutdownFake:
    def __init__(self, events, proof=None):
        self.events = events
        self.proof = proof or CleanupProof(status=CleanupStatus.VERIFIED)

    async def clear_shutdown(self, expectation):
        self.events.append("shutdown")
        return self.proof


class ExecutorFake:
    def __init__(self, result=None):
        self.result = result or executor_result()

    async def import_result(self, operation_id):
        return self.result


class ArtifactFake:
    def __init__(self, events, proof=None):
        self.events = events
        self.proof = proof or CleanupProof(status=CleanupStatus.VERIFIED)
        self.calls = 0

    async def cleanup_executor(self, target):
        self.calls += 1
        self.events.append("artifacts")
        return self.proof


class LockFake:
    def __init__(self, events, proof=None):
        self.events = events
        self.proof = proof or CleanupProof(status=CleanupStatus.VERIFIED)
        self.calls = 0

    async def release(self, *, plan_id, node_id, cluster_ids):
        self.calls += 1
        self.events.append("locks")
        return self.proof


def coordinator(
    *,
    host=None,
    cluster=None,
    allocation=None,
    shutdown=None,
    executor=None,
    artifacts=None,
    locks=None,
    events=None,
):
    events = events if events is not None else []
    return PostReturnCoordinator(
        host=host or HostFake(),
        cluster=cluster or ClusterFake(),
        allocation=allocation or AllocationFake(events),
        shutdown=shutdown or ShutdownFake(events),
        executor_results=executor or ExecutorFake(),
        artifacts=artifacts or ArtifactFake(events),
        locks=locks or LockFake(events),
        clock=lambda: NOW,
    )


class MaintenancePostReturnTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_verifies_return_restores_cluster_then_releases_locks(self):
        events = []
        result = await coordinator(events=events).verify_and_cleanup(request())

        self.assertEqual(result.state, "complete")
        self.assertEqual(result.error_categories, ())
        self.assertTrue(result.executor_result_imported)
        self.assertTrue(result.executor_result_complete)
        self.assertTrue(result.executor_artifacts_cleaned)
        self.assertTrue(result.locks_released)
        self.assertEqual(events, ["allocation", "shutdown", "artifacts", "locks"])
        self.assertTrue(all(check.status == "passed" for check in result.checks))

    async def test_partial_quadlet_return_fails_closed_and_preserves_locks_and_evidence(self):
        events = []
        host = HostFake()
        host.generated = frozenset()
        artifacts = ArtifactFake(events)
        locks = LockFake(events)

        result = await coordinator(
            host=host,
            artifacts=artifacts,
            locks=locks,
            events=events,
        ).verify_and_cleanup(request())

        self.assertEqual(result.state, "recovery_required")
        self.assertIn(PostReturnErrorCategory.REQUIRED_QUADLET_MISSING, result.error_categories)
        self.assertEqual(events, ["allocation", "shutdown"])
        self.assertEqual(artifacts.calls, 0)
        self.assertEqual(locks.calls, 0)

    async def test_wrong_node_identity_version_and_cluster_uuid_are_distinct_failures(self):
        variants = (
            (
                {"persistent_node_id": "different-node"},
                PostReturnErrorCategory.NODE_IDENTITY_MISMATCH,
            ),
            ({"version": "9.2.0"}, PostReturnErrorCategory.NODE_VERSION_MISMATCH),
            ({"cluster_uuid": "different_cluster_uuid"}, PostReturnErrorCategory.CLUSTER_UUID_MISMATCH),
        )
        for changes, expected in variants:
            with self.subTest(expected=expected):
                cluster = ClusterFake()
                cluster.identity = cluster.identity.model_copy(update=changes)
                result = await coordinator(cluster=cluster).verify_and_cleanup(request())
                self.assertEqual(result.state, "recovery_required")
                self.assertIn(expected, result.error_categories)
                self.assertFalse(result.locks_released)

    async def test_allocation_restoration_failure_blocks_artifact_cleanup_and_lock_release(self):
        events = []
        artifacts = ArtifactFake(events)
        locks = LockFake(events)
        result = await coordinator(
            allocation=AllocationFake(events, allocation_result(restored=False)),
            artifacts=artifacts,
            locks=locks,
            events=events,
        ).verify_and_cleanup(request())

        self.assertEqual(result.state, "recovery_required")
        self.assertIn(PostReturnErrorCategory.ALLOCATION_RESTORATION_FAILED, result.error_categories)
        self.assertEqual(events, ["allocation", "shutdown"])
        self.assertFalse(result.allocation_cleanup[0].verified)
        self.assertEqual(artifacts.calls, 0)
        self.assertEqual(locks.calls, 0)

    async def test_shutdown_cleanup_failure_keeps_lock_after_allocation_restoration(self):
        events = []
        artifacts = ArtifactFake(events)
        locks = LockFake(events)
        result = await coordinator(
            shutdown=ShutdownFake(events, CleanupProof(status=CleanupStatus.FAILED)),
            artifacts=artifacts,
            locks=locks,
            events=events,
        ).verify_and_cleanup(request())

        self.assertEqual(result.state, "recovery_required")
        self.assertIn(PostReturnErrorCategory.SHUTDOWN_CLEANUP_FAILED, result.error_categories)
        self.assertEqual(events, ["allocation", "shutdown"])
        self.assertEqual(artifacts.calls, 0)
        self.assertEqual(locks.calls, 0)

    async def test_executor_recovery_result_is_imported_as_evidence_but_not_finalized(self):
        events = []
        artifacts = ArtifactFake(events)
        locks = LockFake(events)
        result = await coordinator(
            executor=ExecutorFake(executor_result(recovery_required=True)),
            artifacts=artifacts,
            locks=locks,
            events=events,
        ).verify_and_cleanup(request())

        self.assertEqual(result.state, "recovery_required")
        self.assertIn(PostReturnErrorCategory.EXECUTOR_RESULT_RECOVERY_REQUIRED, result.error_categories)
        self.assertTrue(result.executor_result_imported)
        self.assertFalse(result.executor_result_complete)
        self.assertEqual(events, ["allocation", "shutdown"])
        self.assertEqual(artifacts.calls, 0)
        self.assertEqual(locks.calls, 0)

    async def test_idempotent_cleanup_accepts_already_clean_proof(self):
        events = []
        already_clean = CleanupProof(status=CleanupStatus.ALREADY_CLEAN)
        result = await coordinator(
            shutdown=ShutdownFake(events, already_clean),
            artifacts=ArtifactFake(events, already_clean),
            locks=LockFake(events, already_clean),
            events=events,
        ).verify_and_cleanup(request())

        self.assertEqual(result.state, "complete")
        self.assertTrue(result.executor_artifacts_cleaned)
        self.assertTrue(result.locks_released)
        self.assertEqual(result.shutdown_cleanup[0].status, CleanupStatus.ALREADY_CLEAN)
        self.assertEqual(events, ["allocation", "shutdown", "artifacts", "locks"])

    async def test_runtime_ownership_rejection_is_stable_and_never_releases_locks(self):
        events = []
        artifacts = ArtifactFake(
            events,
            CleanupProof(status=CleanupStatus.OWNERSHIP_REJECTED),
        )
        locks = LockFake(events)
        result = await coordinator(
            artifacts=artifacts,
            locks=locks,
            events=events,
        ).verify_and_cleanup(request())

        self.assertEqual(result.state, "recovery_required")
        self.assertIn(PostReturnErrorCategory.OWNERSHIP_BOUNDARY_REJECTED, result.error_categories)
        self.assertEqual(events, ["allocation", "shutdown", "artifacts"])
        self.assertEqual(locks.calls, 0)

    def test_cleanup_target_rejects_unowned_paths_and_units_before_io(self):
        with self.assertRaises(ValidationError):
            cleanup_target(paths=("/etc/shadow",))
        with self.assertRaises(ValidationError):
            ExecutorCleanupTarget(
                operation_id=OPERATION_ID,
                unit="ecp-alpha-hot-1.service",
                paths=(),
            )


if __name__ == "__main__":
    unittest.main()
