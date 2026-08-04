from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timedelta, timezone

from app.maintenance_executor import (
    ExecutorCheckResult,
    ExecutorSignature,
    ExecutorUnitResult,
    HostExecutorManifest,
    HostExecutorResult,
    SignedHostExecutorManifest,
    executor_paths,
)
from app.maintenance_lifecycle import MaintenanceState
from app.maintenance_reboot import (
    ClusterGuardSpec,
    ControlAction,
    ExecutorDiscovery,
    ExecutorDiscoveryState,
    ExecutorStageReceipt,
    InvocationAmbiguous,
    PredicateDecision,
    PredicateEvaluation,
    RebootControl,
    RebootInvocationReceipt,
    RebootOrchestrationStatus,
    RebootOrchestrator,
    RebootRequest,
    ReconnectObservation,
    SshDisconnectObservation,
)
from app.modules.maintenance.store import MaintenanceRepository, install_maintenance_schema


NOW = datetime(2026, 8, 3, 0, 0, tzinfo=timezone.utc)
PLAN_ID = "a" * 32
OPERATION_ID = "b" * 32
BOOT_BEFORE = "00000000-1111-2222-3333-444444444444"
BOOT_AFTER = "55555555-6666-7777-8888-999999999999"


def connection_with_plan() -> tuple[sqlite3.Connection, MaintenanceRepository]:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(
        """
        CREATE TABLE users (username TEXT PRIMARY KEY, password_hash TEXT NOT NULL);
        CREATE TABLE nodes (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL);
        CREATE TABLE clusters (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL);
        CREATE TABLE runs (
          id INTEGER PRIMARY KEY,
          status TEXT NOT NULL,
          kind TEXT NOT NULL DEFAULT '',
          target TEXT NOT NULL DEFAULT '',
          context_json TEXT NOT NULL DEFAULT '{}',
          log TEXT NOT NULL DEFAULT '',
          finished_at TEXT
        );
        CREATE TABLE cluster_assignments (
          id INTEGER PRIMARY KEY,
          cluster_id INTEGER NOT NULL REFERENCES clusters(id),
          node_id INTEGER NOT NULL REFERENCES nodes(id),
          revision INTEGER NOT NULL DEFAULT 1,
          operation_run_id INTEGER REFERENCES runs(id)
        );
        CREATE TABLE memberships (
          cluster_id INTEGER NOT NULL REFERENCES clusters(id),
          node_id INTEGER NOT NULL REFERENCES nodes(id),
          PRIMARY KEY(cluster_id,node_id)
        );
        CREATE TABLE host_runtime_observations (
          node_id INTEGER PRIMARY KEY REFERENCES nodes(id),
          initialized INTEGER NOT NULL DEFAULT 0,
          reachable INTEGER NOT NULL DEFAULT 0,
          podman_socket_active INTEGER NOT NULL DEFAULT 0,
          os_name TEXT NOT NULL DEFAULT '',
          podman_version TEXT NOT NULL DEFAULT '',
          observed_at TEXT,
          last_error TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE workload_change_batches (
          run_id INTEGER PRIMARY KEY REFERENCES runs(id),
          cluster_id INTEGER NOT NULL REFERENCES clusters(id),
          plan_encrypted TEXT NOT NULL,
          completed_json TEXT NOT NULL DEFAULT '[]',
          phase TEXT NOT NULL DEFAULT 'applying'
        );
        CREATE TABLE audit_events (
          id INTEGER PRIMARY KEY,
          username TEXT NOT NULL,
          action TEXT NOT NULL,
          cluster_id INTEGER,
          item_id TEXT NOT NULL DEFAULT '',
          detail TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO nodes(id,name) VALUES(1,'node-a');
        INSERT INTO clusters(id,name) VALUES(1,'cluster-a'),(2,'cluster-b');
        """
    )
    install_maintenance_schema(connection)
    repository = MaintenanceRepository(connection)
    repository.create_plan(
        operation_kind="reboot",
        plan={"steps": [{"kind": "safe-reboot"}]},
        idempotency_key="reboot-node-a",
        requested_by="operator",
        expires_at=NOW + timedelta(minutes=5),
        target_node_id=1,
        initial_state=MaintenanceState.READY,
        plan_id=PLAN_ID,
    )
    return connection, repository


def signed_manifest() -> SignedHostExecutorManifest:
    paths = executor_paths(OPERATION_ID)
    manifest = HostExecutorManifest(
        operation_id=OPERATION_ID,
        plan_id=PLAN_ID,
        node_id=1,
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=30),
        pre_reboot_boot_id=BOOT_BEFORE,
        required_units=("ecp-alpha-hot-1.service",),
        checks=(),
        checkpoint_path=str(paths.checkpoint),
        result_path=str(paths.result),
    )
    return SignedHostExecutorManifest(
        manifest=manifest,
        signature=ExecutorSignature(
            key_id="SHA256:" + "A" * 43,
            payload_sha256="c" * 64,
            value="A" * 86,
        ),
    )


def executor_result() -> HostExecutorResult:
    return HostExecutorResult(
        operation_id=OPERATION_ID,
        plan_id=PLAN_ID,
        manifest_hash="c" * 64,
        state="complete",
        reason_code="completed",
        pre_reboot_boot_id=BOOT_BEFORE,
        observed_boot_id=BOOT_AFTER,
        units=(ExecutorUnitResult(unit="ecp-alpha-hot-1.service", active=True),),
        checks=(ExecutorCheckResult(check_id="host-return", passed=True),),
        started_at=NOW,
        completed_at=NOW + timedelta(minutes=1),
    )


class FakeGuard:
    def __init__(self, cluster_id: int, events: list[str]):
        self.cluster_id = cluster_id
        self.events = events

    async def capture(self, *, plan_id: str, cluster_id: int):
        self.events.append(f"capture:{cluster_id}")
        return {"plan_id": plan_id, "cluster_id": cluster_id, "phase": "captured"}

    async def activate(self, checkpoint):
        self.events.append(f"activate:{self.cluster_id}")
        return {"status": "active", "checkpoint": {**checkpoint, "phase": "active"}}

    async def restore(self, checkpoint, *, trigger: str):
        self.events.append(f"restore:{self.cluster_id}:{trigger}")
        return {"status": "restored", "checkpoint": {**checkpoint, "phase": "restored"}}


class FailingCaptureGuard(FakeGuard):
    async def capture(self, *, plan_id: str, cluster_id: int):
        self.events.append(f"capture:{cluster_id}")
        raise RuntimeError("capture unavailable")


class FakePredicates:
    def __init__(self, events: list[str], evaluations: list[PredicateEvaluation] | None = None):
        self.events = events
        self.evaluations = list(evaluations or [passing_predicates(), passing_predicates()])

    async def evaluate(self, *, plan_id: str, node_id: int, stage: str):
        self.events.append(f"predicates:{stage}")
        return self.evaluations.pop(0)


class FakeExecutor:
    def __init__(self, events: list[str]):
        self.events = events

    async def stage(self, envelope: SignedHostExecutorManifest):
        self.events.append("stage")
        return ExecutorStageReceipt(
            operation_id=envelope.manifest.operation_id,
            manifest_hash=envelope.signature.payload_sha256,
            acknowledged=True,
            staged_at=NOW,
        )

    async def discover(self, *, operation_id: str):
        self.events.append("discover")
        return ExecutorDiscovery(
            operation_id=operation_id,
            state=ExecutorDiscoveryState.COMPLETE,
            observed_at=NOW + timedelta(minutes=1),
            result=executor_result(),
        )


class StageFailExecutor(FakeExecutor):
    async def stage(self, envelope: SignedHostExecutorManifest):
        self.events.append("stage")
        raise RuntimeError("sensitive stage detail must not escape")


class DiscoverFailExecutor(FakeExecutor):
    async def discover(self, *, operation_id: str):
        self.events.append("discover")
        raise RuntimeError("sensitive discovery detail must not escape")


class RunningExecutor(FakeExecutor):
    async def discover(self, *, operation_id: str):
        self.events.append("discover")
        return ExecutorDiscovery(
            operation_id=operation_id,
            state=ExecutorDiscoveryState.RUNNING,
            observed_at=NOW + timedelta(minutes=1),
        )


class FakeHost:
    def __init__(
        self,
        events: list[str],
        *,
        invocation: RebootInvocationReceipt | Exception | None = None,
        reconnect_boot_id: str = BOOT_AFTER,
    ):
        self.events = events
        self.invocation = invocation or RebootInvocationReceipt(
            operation_id=OPERATION_ID,
            invocation_id="reboot-call-1",
            acknowledged=True,
            acknowledged_at=NOW,
        )
        self.reconnect_boot_id = reconnect_boot_id

    async def invoke_reboot(self, *, node_id: int, operation_id: str):
        self.events.append("invoke")
        if isinstance(self.invocation, Exception):
            raise self.invocation
        return self.invocation

    async def wait_for_disconnect(self, *, node_id: int, invocation_id: str):
        self.events.append("disconnect")
        return SshDisconnectObservation(disconnected=True, observed_at=NOW + timedelta(seconds=2))

    async def wait_for_reconnect(self, *, node_id: int):
        self.events.append("reconnect")
        return ReconnectObservation(
            connected=True,
            boot_id=self.reconnect_boot_id,
            observed_at=NOW + timedelta(minutes=1),
        )


class ReconnectFailHost(FakeHost):
    async def wait_for_reconnect(self, *, node_id: int):
        self.events.append("reconnect")
        raise RuntimeError("sensitive reconnect detail must not escape")


def passing_predicates() -> PredicateEvaluation:
    return PredicateEvaluation(
        evaluated_at=NOW,
        decisions=(PredicateDecision(identifier="HostReachable", passed=True, evidence="fresh SSH probe"),),
    )


def blocked_predicates() -> PredicateEvaluation:
    return PredicateEvaluation(
        evaluated_at=NOW,
        decisions=(PredicateDecision(identifier="MasterQuorum", passed=False, evidence="quorum would be lost"),),
    )


def request(*guards: FakeGuard) -> RebootRequest:
    return RebootRequest(
        plan_id=PLAN_ID,
        node_id=1,
        pre_reboot_boot_id=BOOT_BEFORE,
        executor_manifest=signed_manifest(),
        clusters=tuple(ClusterGuardSpec(cluster_id=item.cluster_id, guard=item) for item in guards),
    )


class RebootOrchestratorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.connection, self.repository = connection_with_plan()

    def tearDown(self):
        self.connection.close()

    def orchestrator(self, events, **overrides):
        values = {
            "repository": self.repository,
            "predicates": FakePredicates(events),
            "executor": FakeExecutor(events),
            "host": FakeHost(events),
            "control": RebootControl(),
            "execution_enabled": True,
        }
        values.update(overrides)
        return RebootOrchestrator(**values)

    async def test_success_orders_atomic_preparation_acknowledged_reboot_and_discovery(self):
        events: list[str] = []
        guard_two = FakeGuard(2, events)
        guard_one = FakeGuard(1, events)

        result = await self.orchestrator(events).run(request(guard_two, guard_one))

        self.assertEqual(result.status, RebootOrchestrationStatus.READY_FOR_POST_RETURN)
        self.assertEqual(result.boot_id, BOOT_AFTER)
        self.assertEqual(
            events,
            [
                "capture:1", "capture:2", "predicates:prepare", "activate:1", "activate:2",
                "stage", "predicates:reboot", "invoke", "disconnect", "reconnect", "discover",
            ],
        )
        checkpoints = self.repository.list_checkpoints(PLAN_ID)
        self.assertEqual(
            [item.checkpoint_key for item in checkpoints],
            [
                "reboot.cluster.1.captured", "reboot.cluster.2.captured", "reboot.clusters-prepared",
                "reboot.prepare-predicates.1", "reboot.cluster-guards-active", "reboot.executor-staged",
                "reboot.reboot-predicates.1", "reboot.intent", "reboot.invocation-acknowledged",
                "reboot.ssh-disconnected", "reboot.host-reconnected", "reboot.return-discovered",
            ],
        )
        self.assertEqual(self.repository.get_plan(PLAN_ID).lifecycle_state, MaintenanceState.EXECUTING)

    async def test_initialized_empty_host_can_reboot_without_cluster_guards(self):
        events: list[str] = []

        result = await self.orchestrator(events).run(request())

        self.assertEqual(result.status, RebootOrchestrationStatus.READY_FOR_POST_RETURN)
        self.assertEqual(
            events,
            [
                "predicates:prepare", "stage", "predicates:reboot", "invoke",
                "disconnect", "reconnect", "discover",
            ],
        )
        prepared = self.repository.list_checkpoints(PLAN_ID)[0]
        self.assertEqual(prepared.checkpoint_key, "reboot.clusters-prepared")
        self.assertEqual(prepared.payload["cluster_ids"], [])

    async def test_failed_preparation_re_evaluation_causes_no_remote_side_effect(self):
        events: list[str] = []
        guard = FakeGuard(1, events)
        predicates = FakePredicates(events, [blocked_predicates()])

        result = await self.orchestrator(events, predicates=predicates).run(request(guard))

        self.assertEqual(result.status, RebootOrchestrationStatus.BLOCKED)
        self.assertEqual(events, ["capture:1", "predicates:prepare"])
        self.assertEqual(self.repository.get_plan(PLAN_ID).lifecycle_state, MaintenanceState.FAILED)

    async def test_all_cluster_captures_must_succeed_before_any_preparation_is_persisted(self):
        events: list[str] = []

        result = await self.orchestrator(events).run(
            request(FakeGuard(1, events), FailingCaptureGuard(2, events))
        )

        self.assertEqual(result.status, RebootOrchestrationStatus.BLOCKED)
        self.assertEqual(result.reason_code, "cluster-preparation-capture-failed")
        self.assertEqual(events, ["capture:1", "capture:2"])
        self.assertEqual(self.repository.list_checkpoints(PLAN_ID), [])

    async def test_reboot_predicate_failure_restores_active_guards_and_never_invokes_reboot(self):
        events: list[str] = []
        predicates = FakePredicates(events, [passing_predicates(), blocked_predicates()])

        result = await self.orchestrator(events, predicates=predicates).run(
            request(FakeGuard(1, events), FakeGuard(2, events))
        )

        self.assertEqual(result.status, RebootOrchestrationStatus.BLOCKED)
        self.assertNotIn("invoke", events)
        self.assertEqual(
            events[-3:],
            ["predicates:reboot", "restore:2:failure", "restore:1:failure"],
        )
        cleanup = self.repository.latest_checkpoint(PLAN_ID)
        self.assertEqual(cleanup.checkpoint_key, "reboot.cluster-guards-restored-failure")
        self.assertTrue(cleanup.payload["verified"])

    async def test_stage_exception_restores_guards_and_returns_redacted_recovery(self):
        events: list[str] = []

        result = await self.orchestrator(events, executor=StageFailExecutor(events)).run(
            request(FakeGuard(1, events))
        )

        self.assertEqual(result.status, RebootOrchestrationStatus.RECOVERY_REQUIRED)
        self.assertEqual(result.reason_code, "executor-stage-failed")
        self.assertEqual(events[-2:], ["stage", "restore:1:failure"])
        self.assertNotIn("sensitive", repr(result))

    async def test_disconnect_is_never_expected_without_invocation_acknowledgement(self):
        events: list[str] = []
        host = FakeHost(events, invocation=InvocationAmbiguous("transport closed before acknowledgement"))

        result = await self.orchestrator(events, host=host).run(request(FakeGuard(1, events)))

        self.assertEqual(result.status, RebootOrchestrationStatus.RECOVERY_REQUIRED)
        self.assertIn("invoke", events)
        self.assertNotIn("disconnect", events)
        self.assertEqual(self.repository.get_plan(PLAN_ID).lifecycle_state, MaintenanceState.RECOVERY_REQUIRED)
        self.assertEqual(self.repository.latest_checkpoint(PLAN_ID).checkpoint_key, "reboot.intent")

    async def test_unacknowledged_receipt_also_blocks_disconnect_wait(self):
        events: list[str] = []
        host = FakeHost(
            events,
            invocation=RebootInvocationReceipt(
                operation_id=OPERATION_ID,
                invocation_id="reboot-call-1",
                acknowledged=False,
                acknowledged_at=NOW,
            ),
        )

        result = await self.orchestrator(events, host=host).run(request(FakeGuard(1, events)))

        self.assertEqual(result.reason_code, "reboot-invocation-unacknowledged")
        self.assertNotIn("disconnect", events)

    async def test_unchanged_boot_id_requires_recovery_without_executor_assumption(self):
        events: list[str] = []
        host = FakeHost(events, reconnect_boot_id=BOOT_BEFORE)

        result = await self.orchestrator(events, host=host).run(request(FakeGuard(1, events)))

        self.assertEqual(result.status, RebootOrchestrationStatus.RECOVERY_REQUIRED)
        self.assertNotIn("discover", events)
        self.assertEqual(result.reason_code, "boot-id-unchanged")

    async def test_reconnect_exception_is_converted_to_recovery_without_discovery(self):
        events: list[str] = []

        result = await self.orchestrator(events, host=ReconnectFailHost(events)).run(
            request(FakeGuard(1, events))
        )

        self.assertEqual(result.reason_code, "host-return-observation-failed")
        self.assertNotIn("discover", events)
        self.assertNotIn("sensitive", repr(result))

    async def test_discovery_exception_is_converted_to_recovery(self):
        events: list[str] = []

        result = await self.orchestrator(events, executor=DiscoverFailExecutor(events)).run(
            request(FakeGuard(1, events))
        )

        self.assertEqual(result.reason_code, "executor-discovery-failed")
        self.assertEqual(events[-1], "discover")
        self.assertNotIn("sensitive", repr(result))

    async def test_running_executor_is_not_mistaken_for_completed_return(self):
        events: list[str] = []

        result = await self.orchestrator(events, executor=RunningExecutor(events)).run(
            request(FakeGuard(1, events))
        )

        self.assertEqual(result.status, RebootOrchestrationStatus.RECOVERY_REQUIRED)
        self.assertEqual(result.reason_code, "executor-state-unverified")
        self.assertEqual(result.executor_state, ExecutorDiscoveryState.RUNNING)

    async def test_completed_run_is_idempotent_and_does_not_repeat_remote_calls(self):
        events: list[str] = []
        orchestrator = self.orchestrator(events)
        reboot_request = request(FakeGuard(1, events))
        first = await orchestrator.run(reboot_request)
        first_events = tuple(events)

        second = await orchestrator.run(reboot_request)

        self.assertEqual(first, second)
        self.assertEqual(tuple(events), first_events)

    async def test_cancel_at_safe_pre_side_effect_checkpoint_stops_without_remote_mutation(self):
        events: list[str] = []
        control = RebootControl({"clusters-prepared": ControlAction.CANCEL})

        result = await self.orchestrator(events, control=control).run(request(FakeGuard(1, events)))

        self.assertEqual(result.status, RebootOrchestrationStatus.CANCELLED)
        self.assertEqual(events, ["capture:1"])
        self.assertEqual(self.repository.get_plan(PLAN_ID).lifecycle_state, MaintenanceState.CANCELLED)

    async def test_pause_is_honored_only_at_safe_checkpoint_and_resume_is_idempotent(self):
        events: list[str] = []
        control = RebootControl({"clusters-prepared": ControlAction.PAUSE})
        orchestrator = self.orchestrator(events, control=control)
        reboot_request = request(FakeGuard(1, events))

        paused = await orchestrator.run(reboot_request)
        self.assertEqual(paused.status, RebootOrchestrationStatus.PAUSED)
        self.assertEqual(events, ["capture:1"])

        control.clear("clusters-prepared")
        resumed = await orchestrator.run(reboot_request, resume=True)
        self.assertEqual(resumed.status, RebootOrchestrationStatus.READY_FOR_POST_RETURN)
        self.assertEqual(events.count("capture:1"), 1)
        self.assertEqual(events.count("invoke"), 1)

    async def test_controller_restart_handoff_never_replays_ambiguous_intent(self):
        events: list[str] = []
        self.repository.transition_plan(PLAN_ID, 1, MaintenanceState.EXECUTING)
        self.repository.record_checkpoint(
            plan_id=PLAN_ID,
            checkpoint_key="reboot.intent",
            sequence=700,
            side_effect_state="prepared",
            payload={"operation_id": OPERATION_ID, "node_id": 1},
            observation={"pre_reboot_boot_id": BOOT_BEFORE},
        )
        self.repository.prepare_startup_recovery()
        orchestrator = self.orchestrator(events)

        handoff = orchestrator.recovery_handoff(PLAN_ID)
        result = await orchestrator.run(request(FakeGuard(1, events)), resume=True)

        self.assertFalse(handoff.resume_allowed)
        self.assertTrue(handoff.observation_required)
        self.assertEqual(handoff.latest_checkpoint, "reboot.intent")
        self.assertEqual(result.status, RebootOrchestrationStatus.RECOVERY_REQUIRED)
        self.assertEqual(events, [])

    async def test_execution_is_disabled_by_default(self):
        events: list[str] = []
        orchestrator = RebootOrchestrator(
            repository=self.repository,
            predicates=FakePredicates(events),
            executor=FakeExecutor(events),
            host=FakeHost(events),
        )

        with self.assertRaisesRegex(RuntimeError, "disabled"):
            await orchestrator.run(request(FakeGuard(1, events)))
        self.assertEqual(events, [])


if __name__ == "__main__":
    unittest.main()
