from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timedelta, timezone

from app.modules.maintenance.container_maintenance import ManagedContainerTarget, RuntimeActionResult
from app.modules.maintenance.execution import MaintenanceExecutionService
from app.modules.maintenance.host_maintenance import (
    HostMaintenanceService,
    _start_priority,
    _stop_priority,
)
from app.modules.maintenance.lifecycle import HostMaintenanceState, MaintenanceState
from app.modules.maintenance.planned_contracts import MaintenanceWorkflowState
from app.modules.maintenance.post_return import (
    ClusterExpectation,
    EndpointExpectation,
    HostMaintenancePostReturnResult,
    NodeIdentityExpectation,
    PostReturnErrorCategory,
    PostReturnExpectations,
    ServiceBudgetExpectation,
)
from app.modules.maintenance.store import MaintenanceRepository, install_maintenance_schema


NOW = datetime(2026, 8, 5, 9, 0, tzinfo=timezone.utc)


def connection() -> sqlite3.Connection:
    value = sqlite3.connect(":memory:")
    value.row_factory = sqlite3.Row
    value.execute("PRAGMA foreign_keys = ON")
    value.executescript("""
        CREATE TABLE nodes (
          id INTEGER PRIMARY KEY,
          name TEXT UNIQUE NOT NULL,
          enabled INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE clusters (
          id INTEGER PRIMARY KEY,
          name TEXT UNIQUE NOT NULL,
          slug TEXT NOT NULL UNIQUE
        );
        CREATE TABLE memberships (
          cluster_id INTEGER NOT NULL REFERENCES clusters(id),
          node_id INTEGER NOT NULL REFERENCES nodes(id),
          PRIMARY KEY(cluster_id, node_id)
        );
        CREATE TABLE runs (
          id INTEGER PRIMARY KEY,
          kind TEXT NOT NULL,
          target TEXT NOT NULL,
          status TEXT NOT NULL,
          command_json TEXT NOT NULL,
          log TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          finished_at TEXT,
          context_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE cluster_assignments (
          id INTEGER PRIMARY KEY,
          cluster_id INTEGER NOT NULL REFERENCES clusters(id),
          node_id INTEGER NOT NULL REFERENCES nodes(id),
          role TEXT NOT NULL,
          revision INTEGER NOT NULL DEFAULT 1,
          state TEXT NOT NULL DEFAULT 'active',
          operation_run_id INTEGER REFERENCES runs(id)
        );
        CREATE TABLE workload_change_batches (
          run_id INTEGER PRIMARY KEY REFERENCES runs(id),
          cluster_id INTEGER NOT NULL REFERENCES clusters(id)
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
        INSERT INTO nodes(id,name,enabled) VALUES(1,'node-a',1),(2,'node-b',1);
        INSERT INTO clusters(id,name,slug) VALUES(1,'cluster-a','alpha'),(2,'cluster-b','beta');
        INSERT INTO memberships(cluster_id,node_id) VALUES(1,1),(2,1),(1,2);
        INSERT INTO cluster_assignments(id,cluster_id,node_id,role,revision,state) VALUES
          (11,1,1,'hot',4,'active'),
          (12,1,1,'kibana',2,'active'),
          (13,2,1,'master',3,'active'),
          (14,1,2,'warm',4,'active');
    """)
    install_maintenance_schema(value)
    return value


class FakeRuntime:
    def __init__(self):
        self.calls: list[tuple[str, int | None, str]] = []

    async def stop(self, target: ManagedContainerTarget) -> RuntimeActionResult:
        self.calls.append(("stop", target.assignment_id, target.unit))
        return RuntimeActionResult(confirmed=True)

    async def start(self, target: ManagedContainerTarget) -> RuntimeActionResult:
        self.calls.append(("start", target.assignment_id, target.unit))
        return RuntimeActionResult(confirmed=True)

    async def ready(self, target: ManagedContainerTarget) -> RuntimeActionResult:
        self.calls.append(("ready", target.assignment_id, target.unit))
        return RuntimeActionResult(confirmed=True)

    async def host_ready(self, node_id: int) -> RuntimeActionResult:
        self.calls.append(("host_ready", None, str(node_id)))
        return RuntimeActionResult(confirmed=True)


class StopFailureRuntime(FakeRuntime):
    async def stop(self, target: ManagedContainerTarget) -> RuntimeActionResult:
        self.calls.append(("stop", target.assignment_id, target.unit))
        return RuntimeActionResult(confirmed=target.assignment_id != 11)


class FakeCompanions:
    def __init__(self):
        self.assignment_ids: list[int] = []

    async def reconcile(self, *, assignment_id: int, run_id: int) -> RuntimeActionResult:
        self.assignment_ids.append(assignment_id)
        return RuntimeActionResult(confirmed=True)


class FakeGuard:
    def __init__(self):
        self.calls: list[tuple[str, str, int]] = []

    async def capture(self, *, plan_id: str, cluster_id: int):
        self.calls.append(("capture", plan_id, cluster_id))

    async def activate(self, *, plan_id: str, cluster_id: int):
        self.calls.append(("activate", plan_id, cluster_id))

    async def restore(self, *, plan_id: str, cluster_id: int):
        self.calls.append(("restore", plan_id, cluster_id))


class FakePostReturn:
    def __init__(self, *, recovery_required=False):
        self.recovery_required = recovery_required
        self.requests = []

    async def verify_host_maintenance(self, request):
        self.requests.append(request)
        return HostMaintenancePostReturnResult(
            state="recovery_required" if self.recovery_required else "complete",
            checks=(),
            error_categories=(PostReturnErrorCategory.NODE_VERSION_MISMATCH,)
            if self.recovery_required
            else (),
        )


class FakeRebootExecutor:
    def __init__(self, *, confirmed=True, cleanup_confirmed=True):
        self.confirmed = confirmed
        self.cleanup_confirmed = cleanup_confirmed
        self.calls: list[tuple[str, str, tuple[int, ...]]] = []

    async def reboot(self, *, plan, targets):
        self.calls.append(("reboot", plan.id, tuple(target.assignment_id for target in targets)))
        return RuntimeActionResult(confirmed=self.confirmed)

    async def cleanup(self, *, plan):
        self.calls.append(("cleanup", plan.id, ()))
        return RuntimeActionResult(confirmed=self.cleanup_confirmed)


class HostMaintenanceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.connection = connection()
        self.repository = MaintenanceRepository(self.connection)
        self.execution = MaintenanceExecutionService(
            self.repository,
            capability_revision=lambda: "cap-v1",
            clock=lambda: NOW,
        )
        self.addCleanup(self.connection.close)

    def plan(self, *, include_post_return_expectations=True):
        target_manifest = {
            "public_operation": "host_maintenance",
            "affected_cluster_ids": [1, 2],
            "assignment_revisions": [
                {"assignment_id": 11, "revision": 4},
                {"assignment_id": 12, "revision": 2},
                {"assignment_id": 13, "revision": 3},
            ],
        }
        if include_post_return_expectations:
            target_manifest["post_return_expectations"] = PostReturnExpectations(
                endpoints=(EndpointExpectation(endpoint_ref="kibana-status"),),
                clusters=(
                    ClusterExpectation(
                        cluster_id=1,
                        nodes=(
                            NodeIdentityExpectation(
                                cluster_id=1,
                                assignment_id=11,
                                persistent_node_id="persistent-node-11",
                                node_name="ecp-alpha-hot-1",
                                version="8.19.0",
                                cluster_uuid="cluster_uuid_alpha",
                            ),
                        ),
                    ),
                    ClusterExpectation(
                        cluster_id=2,
                        nodes=(
                            NodeIdentityExpectation(
                                cluster_id=2,
                                assignment_id=13,
                                persistent_node_id="persistent-node-13",
                                node_name="ecp-beta-master-1",
                                version="8.19.0",
                                cluster_uuid="cluster_uuid_beta",
                            ),
                        ),
                    ),
                ),
                service_budgets=(
                    ServiceBudgetExpectation(cluster_id=1, role="kibana", minimum_available=1),
                    ServiceBudgetExpectation(cluster_id=2, role="master", minimum_available=2),
                ),
            ).model_dump(mode="json")
        return self.repository.create_plan(
            operation_kind="reboot",
            plan={"policy": {"observation_max_age_seconds": 120}},
            observation={
                "captured_at": NOW.isoformat().replace("+00:00", "Z"),
                "capability_revision": "cap-v1",
            },
            idempotency_key="host-maintenance",
            requested_by="operator",
            expires_at=NOW + timedelta(minutes=5),
            target_node_id=1,
            target_manifest=target_manifest,
            initial_state=MaintenanceState.READY,
        )

    @staticmethod
    def targets(_plan):
        return (
            ManagedContainerTarget(11, 1, 1, "hot", "ecp-alpha-hot-1.service", True),
            ManagedContainerTarget(12, 1, 1, "kibana", "ecp-alpha-kibana-1.service", False),
            ManagedContainerTarget(13, 2, 1, "master", "ecp-beta-master-1.service", False),
        )

    async def test_cross_cluster_host_flow_preserves_dependency_order_and_peer_isolation(self):
        plan = self.plan()
        runtime = FakeRuntime()
        companions = FakeCompanions()
        guard = FakeGuard()
        post_return = FakePostReturn()
        service = HostMaintenanceService(
            self.repository,
            execution=self.execution,
            targets_resolver=self.targets,
            runtime=runtime,
            companions=companions,
            allocation_guard=guard,
            post_return=post_return,
        )

        prepared = await service.prepare(plan.id, username="operator")
        self.assertEqual(prepared.workflow_state, MaintenanceWorkflowState.READY_TO_STOP)
        self.assertEqual(guard.calls, [("capture", plan.id, 1), ("activate", plan.id, 1)])
        run_id = self.repository.get_plan(plan.id).run_id
        claimed = self.connection.execute(
            "SELECT id,operation_run_id FROM cluster_assignments ORDER BY id",
        ).fetchall()
        self.assertEqual(
            [(row["id"], row["operation_run_id"]) for row in claimed],
            [(11, run_id), (12, run_id), (13, run_id), (14, None)],
        )

        ready_for_host = await service.stop_workloads(plan.id, username="operator")
        self.assertEqual(ready_for_host.workflow_state, MaintenanceWorkflowState.MAINTENANCE)
        self.assertEqual(runtime.calls, [
            ("stop", 12, "ecp-alpha-kibana-1.service"),
            ("stop", 11, "ecp-alpha-hot-1.service"),
            ("stop", 13, "ecp-beta-master-1.service"),
        ])

        await service.record_operator_handoff(plan.id, username="operator")
        returned = await service.return_to_service(plan.id, username="operator")
        self.assertEqual(returned.workflow_state, MaintenanceWorkflowState.AVAILABLE)
        self.assertEqual(runtime.calls, [
            ("stop", 12, "ecp-alpha-kibana-1.service"),
            ("stop", 11, "ecp-alpha-hot-1.service"),
            ("stop", 13, "ecp-beta-master-1.service"),
            ("host_ready", None, "1"),
            ("start", 13, "ecp-beta-master-1.service"),
            ("ready", 13, "ecp-beta-master-1.service"),
            ("start", 11, "ecp-alpha-hot-1.service"),
            ("ready", 11, "ecp-alpha-hot-1.service"),
            ("start", 12, "ecp-alpha-kibana-1.service"),
            ("ready", 12, "ecp-alpha-kibana-1.service"),
        ])
        self.assertEqual(companions.assignment_ids, [13, 11, 12])
        self.assertEqual(guard.calls[-1], ("restore", plan.id, 1))
        self.assertEqual(
            [item.unit for item in post_return.requests[0].workloads],
            ["ecp-alpha-hot-1.service", "ecp-alpha-kibana-1.service", "ecp-beta-master-1.service"],
        )
        self.assertEqual(
            [item.cluster_id for item in post_return.requests[0].clusters],
            [1, 2],
        )
        self.assertEqual(self.repository.get_plan(plan.id).lifecycle_state, MaintenanceState.SUCCEEDED)
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM maintenance_locks WHERE released_at IS NULL").fetchone()[0],
            0,
        )
        retained_peer = self.connection.execute(
            "SELECT operation_run_id FROM cluster_assignments WHERE id=14",
        ).fetchone()["operation_run_id"]
        self.assertIsNone(retained_peer)

    async def test_reboot_action_requires_a_confirmed_signed_executor_then_cleans_it(self):
        plan = self.plan()
        reboot = FakeRebootExecutor()
        service = HostMaintenanceService(
            self.repository,
            execution=self.execution,
            targets_resolver=self.targets,
            runtime=FakeRuntime(),
            companions=FakeCompanions(),
            allocation_guard=FakeGuard(),
            post_return=FakePostReturn(),
            reboot_executor=reboot,
        )

        await service.prepare(plan.id, username="operator")
        await service.stop_workloads(plan.id, username="operator")
        rebooted = await service.reboot_host(plan.id, username="operator")
        returned = await service.return_to_service(plan.id, username="operator")

        self.assertEqual(rebooted.workflow_state, MaintenanceWorkflowState.MAINTENANCE)
        self.assertEqual(returned.workflow_state, MaintenanceWorkflowState.AVAILABLE)
        self.assertEqual(reboot.calls, [
            ("reboot", plan.id, (11, 12, 13)),
            ("cleanup", plan.id, ()),
        ])
        checkpoints = self.repository.list_checkpoints(plan.id)
        self.assertIn("host:reboot:host", [item.checkpoint_key for item in checkpoints])
        self.assertIn("host:executor-cleanup-complete:host", [item.checkpoint_key for item in checkpoints])

    async def test_reboot_action_fails_closed_without_a_configured_executor(self):
        plan = self.plan()
        service = HostMaintenanceService(
            self.repository,
            execution=self.execution,
            targets_resolver=self.targets,
            runtime=FakeRuntime(),
            companions=FakeCompanions(),
            allocation_guard=FakeGuard(),
            post_return=FakePostReturn(),
        )

        await service.prepare(plan.id, username="operator")
        await service.stop_workloads(plan.id, username="operator")
        with self.assertRaisesRegex(RuntimeError, "not configured"):
            await service.reboot_host(plan.id, username="operator")

        self.assertEqual(self.repository.get_plan(plan.id).lifecycle_state, MaintenanceState.EXECUTING)
        self.assertEqual(self.repository.get_host_state(1).workflow_state, MaintenanceWorkflowState.MAINTENANCE)

    async def test_prepare_requires_immutable_post_return_expectations_before_creating_a_run(self):
        plan = self.plan(include_post_return_expectations=False)
        service = HostMaintenanceService(
            self.repository,
            execution=self.execution,
            targets_resolver=self.targets,
            runtime=FakeRuntime(),
            companions=FakeCompanions(),
            allocation_guard=FakeGuard(),
        )

        with self.assertRaisesRegex(RuntimeError, "post-return expectations"):
            await service.prepare(plan.id, username="operator")

        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 0)
        self.assertEqual(self.repository.list_active_locks(plan.id), [])

    async def test_prepare_rejects_a_target_set_that_does_not_cover_every_planned_assignment(self):
        plan = self.plan()
        service = HostMaintenanceService(
            self.repository,
            execution=self.execution,
            targets_resolver=lambda _plan: self.targets(_plan)[:2],
            runtime=FakeRuntime(),
            companions=FakeCompanions(),
            allocation_guard=FakeGuard(),
        )

        with self.assertRaisesRegex(RuntimeError, "target set"):
            await service.prepare(plan.id, username="operator")

        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 0)
        self.assertEqual(self.repository.list_active_locks(plan.id), [])

    async def test_stop_failure_preserves_host_and_assignment_recovery_ownership(self):
        plan = self.plan()
        runtime = StopFailureRuntime()
        guard = FakeGuard()
        service = HostMaintenanceService(
            self.repository,
            execution=self.execution,
            targets_resolver=self.targets,
            runtime=runtime,
            companions=FakeCompanions(),
            allocation_guard=guard,
            post_return=FakePostReturn(),
        )

        await service.prepare(plan.id, username="operator")
        run_id = self.repository.get_plan(plan.id).run_id

        with self.assertRaisesRegex(RuntimeError, "stop was not confirmed"):
            await service.stop_workloads(plan.id, username="operator")

        self.assertEqual(runtime.calls, [
            ("stop", 12, "ecp-alpha-kibana-1.service"),
            ("stop", 11, "ecp-alpha-hot-1.service"),
        ])
        self.assertEqual(self.repository.get_host_state(1).state, HostMaintenanceState.RECOVERY_REQUIRED)
        self.assertEqual(self.repository.get_plan(plan.id).lifecycle_state, MaintenanceState.RECOVERY_REQUIRED)
        self.assertTrue(self.repository.list_active_locks(plan.id))
        self.assertEqual(guard.calls, [("capture", plan.id, 1), ("activate", plan.id, 1)])
        self.assertEqual(
            [self.repository.get_assignment_state(assignment_id).workflow_state for assignment_id in (11, 12, 13)],
            [MaintenanceWorkflowState.RECOVERY_REQUIRED] * 3,
        )
        claimed = self.connection.execute(
            "SELECT id,operation_run_id FROM cluster_assignments ORDER BY id",
        ).fetchall()
        self.assertEqual(
            [(row["id"], row["operation_run_id"]) for row in claimed],
            [(11, run_id), (12, run_id), (13, run_id), (14, None)],
        )

    async def test_return_identity_failure_restores_allocation_then_requires_recovery(self):
        plan = self.plan()
        guard = FakeGuard()
        post_return = FakePostReturn(recovery_required=True)
        service = HostMaintenanceService(
            self.repository,
            execution=self.execution,
            targets_resolver=self.targets,
            runtime=FakeRuntime(),
            companions=FakeCompanions(),
            allocation_guard=guard,
            post_return=post_return,
        )

        await service.prepare(plan.id, username="operator")
        await service.stop_workloads(plan.id, username="operator")

        with self.assertRaisesRegex(RuntimeError, "post-return verification"):
            await service.return_to_service(plan.id, username="operator")

        self.assertEqual(guard.calls[-1], ("restore", plan.id, 1))
        self.assertEqual(self.repository.get_plan(plan.id).lifecycle_state, MaintenanceState.RECOVERY_REQUIRED)
        self.assertEqual(self.repository.get_host_state(1).state, HostMaintenanceState.RECOVERY_REQUIRED)
        self.assertEqual(
            [self.repository.get_assignment_state(assignment_id).workflow_state for assignment_id in (11, 12, 13)],
            [MaintenanceWorkflowState.RECOVERY_REQUIRED] * 3,
        )

    async def test_host_flow_reports_fixed_redacted_progress_messages(self):
        plan = self.plan()
        progress = []
        service = HostMaintenanceService(
            self.repository,
            execution=self.execution,
            targets_resolver=self.targets,
            runtime=FakeRuntime(),
            companions=FakeCompanions(),
            allocation_guard=FakeGuard(),
            post_return=FakePostReturn(),
            progress=lambda run_id, message: progress.append((run_id, message)),
        )

        await service.prepare(plan.id, username="operator")
        await service.stop_workloads(plan.id, username="operator")
        await service.record_operator_handoff(plan.id, username="operator")
        await service.return_to_service(plan.id, username="operator")

        run_id = self.repository.get_plan(plan.id).run_id
        self.assertEqual(progress, [
            (run_id, "Preparing managed workloads on the selected host.\n"),
            (run_id, "Stopping managed workloads on the selected host.\n"),
            (run_id, "Host maintenance is ready for the operator handoff.\n"),
            (run_id, "Rediscovering the selected host before workload return.\n"),
            (run_id, "Returning managed workloads on the selected host to service.\n"),
            (run_id, "Verifying the returned host and affected clusters.\n"),
        ])

    def test_dependency_order_covers_data_master_and_dependent_stack_roles(self):
        targets = (
            ManagedContainerTarget(11, 1, 1, "hot", "ecp-alpha-hot-1.service", True),
            ManagedContainerTarget(12, 1, 1, "kibana", "ecp-alpha-kibana-1.service", False),
            ManagedContainerTarget(13, 1, 1, "master", "ecp-alpha-master-1.service", False),
            ManagedContainerTarget(15, 1, 1, "fleet-server", "ecp-alpha-fleet-server-1.service", False),
            ManagedContainerTarget(16, 1, 1, "logstash", "ecp-alpha-logstash-1.service", False),
            ManagedContainerTarget(17, 1, 1, "elastic-agent", "ecp-alpha-elastic-agent-1.service", False),
        )

        self.assertEqual(
            [item.role for item in sorted(targets, key=_stop_priority)],
            ["elastic-agent", "kibana", "fleet-server", "logstash", "hot", "master"],
        )
        self.assertEqual(
            [item.role for item in sorted(targets, key=_start_priority)],
            ["master", "hot", "kibana", "fleet-server", "logstash", "elastic-agent"],
        )


if __name__ == "__main__":
    unittest.main()
