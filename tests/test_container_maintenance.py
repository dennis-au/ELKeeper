from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.modules.maintenance.container_maintenance import (
    ContainerMaintenanceService,
    ControllerManagedWorkloadRuntime,
    ManagedContainerTarget,
    RuntimeActionResult,
    resolve_managed_container_target,
)
from app.modules.maintenance.execution import MaintenanceExecutionService
from app.modules.maintenance.lifecycle import MaintenanceState
from app.modules.maintenance.planned_contracts import MaintenanceWorkflowState
from app.modules.maintenance.store import MaintenanceRepository, install_maintenance_schema
from app.modules.workloads import WorkloadRepository


NOW = datetime(2026, 8, 5, 8, 0, tzinfo=timezone.utc)


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
        INSERT INTO nodes(id,name,enabled) VALUES(1,'node-a',1);
        INSERT INTO clusters(id,name,slug) VALUES(1,'cluster-a','demo');
        INSERT INTO memberships(cluster_id,node_id) VALUES(1,1);
        INSERT INTO cluster_assignments(id,cluster_id,node_id,role,revision,state)
          VALUES(11,1,1,'hot',4,'active'),(12,1,1,'kibana',2,'active');
    """)
    install_maintenance_schema(value)
    return value


class FakeRuntime:
    def __init__(self, *, stop_confirmed: bool = True, start_confirmed: bool = True, ready: bool = True):
        self.stop_confirmed = stop_confirmed
        self.start_confirmed = start_confirmed
        self.ready_confirmed = ready
        self.calls: list[tuple[str, int, str]] = []

    async def stop(self, target: ManagedContainerTarget) -> RuntimeActionResult:
        self.calls.append(("stop", target.assignment_id, target.unit))
        return RuntimeActionResult(confirmed=self.stop_confirmed, detail="stop")

    async def start(self, target: ManagedContainerTarget) -> RuntimeActionResult:
        self.calls.append(("start", target.assignment_id, target.unit))
        return RuntimeActionResult(confirmed=self.start_confirmed, detail="start")

    async def ready(self, target: ManagedContainerTarget) -> RuntimeActionResult:
        self.calls.append(("ready", target.assignment_id, target.unit))
        return RuntimeActionResult(confirmed=self.ready_confirmed, detail="ready")


class FakeCompanions:
    def __init__(self):
        self.assignment_ids: list[int] = []

    async def reconcile(self, *, assignment_id: int, run_id: int) -> RuntimeActionResult:
        self.assignment_ids.append(assignment_id)
        return RuntimeActionResult(confirmed=True, detail=f"run-{run_id}")


class FakeGuard:
    def __init__(self):
        self.calls: list[tuple[str, str, int]] = []

    async def capture(self, *, plan_id: str, cluster_id: int):
        self.calls.append(("capture", plan_id, cluster_id))

    async def activate(self, *, plan_id: str, cluster_id: int):
        self.calls.append(("activate", plan_id, cluster_id))

    async def restore(self, *, plan_id: str, cluster_id: int):
        self.calls.append(("restore", plan_id, cluster_id))


class FakeControllerIO:
    def __init__(self, *, stop_result: bool = True, start_result: bool = True, ready: bool = True):
        self.stop_result = stop_result
        self.start_result = start_result
        self.ready = ready
        self.calls: list[tuple[str, int, str]] = []

    async def stop_managed_unit(self, *, node_id: int, unit: str) -> bool:
        self.calls.append(("stop", node_id, unit))
        return self.stop_result

    async def start_managed_unit(self, *, node_id: int, unit: str) -> bool:
        self.calls.append(("start", node_id, unit))
        return self.start_result

    async def unit_states(self, *, node_id: int, units: tuple[str, ...]):
        self.calls.append(("ready", node_id, units[0]))
        return {units[0]: self.ready}


class ContainerMaintenanceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.connection = connection()
        self.repository = MaintenanceRepository(self.connection)
        self.execution = MaintenanceExecutionService(
            self.repository,
            capability_revision=lambda: "cap-v1",
            clock=lambda: NOW,
        )
        self.addCleanup(self.connection.close)

    def plan(self, assignment_id: int = 11):
        row = self.connection.execute(
            "SELECT cluster_id,node_id,revision FROM cluster_assignments WHERE id=?", (assignment_id,),
        ).fetchone()
        return self.repository.create_plan(
            operation_kind="workload_restart",
            plan={"policy": {"observation_max_age_seconds": 120}},
            observation={
                "captured_at": NOW.isoformat().replace("+00:00", "Z"),
                "capability_revision": "cap-v1",
            },
            idempotency_key=f"container-{assignment_id}",
            requested_by="operator",
            expires_at=NOW + timedelta(minutes=5),
            target_node_id=int(row["node_id"]),
            target_cluster_id=int(row["cluster_id"]),
            target_assignment_id=assignment_id,
            target_manifest={
                "public_operation": "container_maintenance",
                "affected_cluster_ids": [int(row["cluster_id"])],
                "assignment_revisions": [{"assignment_id": assignment_id, "revision": int(row["revision"])}],
            },
            initial_state=MaintenanceState.READY,
        )

    @staticmethod
    def target(assignment_id: int = 11) -> ManagedContainerTarget:
        role = "hot" if assignment_id == 11 else "kibana"
        return ManagedContainerTarget(
            assignment_id=assignment_id,
            cluster_id=1,
            node_id=1,
            role=role,
            unit=f"ecp-demo-{role}-1.service",
            data_bearing=role == "hot",
        )

    def test_public_target_resolver_uses_the_assignment_identity_and_exact_unit(self):
        target = resolve_managed_container_target(self.connection, 11)

        self.assertEqual(target.assignment_id, 11)
        self.assertEqual(target.unit, "ecp-demo-hot-1.service")
        self.assertTrue(target.data_bearing)

    async def test_controller_runtime_uses_only_the_selected_managed_unit(self):
        target = self.target()
        io = FakeControllerIO(ready=False)
        runtime = ControllerManagedWorkloadRuntime(io)

        self.assertTrue((await runtime.stop(target)).confirmed)
        self.assertTrue((await runtime.start(target)).confirmed)
        self.assertFalse((await runtime.ready(target)).confirmed)
        self.assertEqual(io.calls, [
            ("stop", 1, "ecp-demo-hot-1.service"),
            ("start", 1, "ecp-demo-hot-1.service"),
            ("ready", 1, "ecp-demo-hot-1.service"),
        ])

    async def test_data_container_flow_is_assignment_scoped_and_restores_guard(self):
        plan = self.plan()
        runtime = FakeRuntime()
        companions = FakeCompanions()
        guard = FakeGuard()
        service = ContainerMaintenanceService(
            self.repository,
            execution=self.execution,
            target_resolver=lambda assignment_id: self.target(assignment_id),
            runtime=runtime,
            companions=companions,
            allocation_guard=guard,
        )

        prepared = await service.prepare(plan.id, username="operator")

        self.assertEqual(prepared.workflow_state, MaintenanceWorkflowState.READY_TO_STOP)
        self.assertEqual(guard.calls, [("capture", plan.id, 1), ("activate", plan.id, 1)])
        run_id = self.repository.get_plan(plan.id).run_id
        self.assertIsNotNone(run_id)
        claimed = self.connection.execute(
            "SELECT id,operation_run_id FROM cluster_assignments ORDER BY id",
        ).fetchall()
        self.assertEqual([(item["id"], item["operation_run_id"]) for item in claimed], [(11, run_id), (12, None)])
        self.assertEqual(
            {(item.scope.value, item.identifier) for item in self.repository.list_active_locks(plan.id)},
            {("host", "1"), ("cluster", "1"), ("assignment", "11")},
        )

        stopped = await service.stop(plan.id, username="operator")
        self.assertEqual(stopped.workflow_state, MaintenanceWorkflowState.MAINTENANCE)
        self.assertEqual(runtime.calls, [("stop", 11, "ecp-demo-hot-1.service")])

        returned = await service.return_to_service(plan.id, username="operator")
        self.assertEqual(returned.workflow_state, MaintenanceWorkflowState.AVAILABLE)
        self.assertEqual(runtime.calls, [
            ("stop", 11, "ecp-demo-hot-1.service"),
            ("start", 11, "ecp-demo-hot-1.service"),
            ("ready", 11, "ecp-demo-hot-1.service"),
        ])
        self.assertEqual(companions.assignment_ids, [11])
        self.assertEqual(guard.calls[-1], ("restore", plan.id, 1))
        self.assertEqual(self.repository.get_plan(plan.id).lifecycle_state, MaintenanceState.SUCCEEDED)
        self.assertEqual(self.repository.list_active_locks(plan.id), [])
        self.assertIsNone(self.connection.execute(
            "SELECT operation_run_id FROM cluster_assignments WHERE id=11",
        ).fetchone()["operation_run_id"])
        self.assertIsNone(self.connection.execute(
            "SELECT operation_run_id FROM cluster_assignments WHERE id=12",
        ).fetchone()["operation_run_id"])

    async def test_flow_emits_only_fixed_progress_messages_to_the_attached_run(self):
        plan = self.plan()

        def progress(run_id: int, message: str) -> None:
            self.connection.execute("UPDATE runs SET log=log || ? WHERE id=?", (message, run_id))

        service = ContainerMaintenanceService(
            self.repository,
            execution=self.execution,
            target_resolver=lambda assignment_id: self.target(assignment_id),
            runtime=FakeRuntime(),
            companions=FakeCompanions(),
            allocation_guard=FakeGuard(),
            progress=progress,
        )

        await service.prepare(plan.id, username="operator")
        await service.stop(plan.id, username="operator")
        await service.return_to_service(plan.id, username="operator")

        row = self.connection.execute(
            "SELECT id,log FROM runs WHERE id=?", (self.repository.get_plan(plan.id).run_id,),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(
            row["log"],
            "Preparing selected managed workload.\n"
            "Capturing and activating the data-node allocation guard.\n"
            "Selected managed workload is ready to stop.\n"
            "Stopping selected managed workload.\n"
            "Returning selected managed workload to service.\n"
            "Verifying selected managed workload readiness.\n"
            "Scheduling selected workload companion reconciliation.\n",
        )
        self.assertNotIn("ecp-", row["log"])
        self.assertNotIn("password", row["log"].lower())

    async def test_return_claim_release_failure_requires_recovery_before_finalizing_the_run(self):
        plan = self.plan()
        service = ContainerMaintenanceService(
            self.repository,
            execution=self.execution,
            target_resolver=lambda assignment_id: self.target(assignment_id),
            runtime=FakeRuntime(),
            companions=FakeCompanions(),
            allocation_guard=FakeGuard(),
        )
        await service.prepare(plan.id, username="operator")
        await service.stop(plan.id, username="operator")

        with patch.object(
            WorkloadRepository,
            "release_assignment_operation_in_connection",
            return_value=False,
        ):
            with self.assertRaisesRegex(RuntimeError, "claim could not be released"):
                await service.return_to_service(plan.id, username="operator")

        self.assertEqual(self.repository.get_plan(plan.id).lifecycle_state, MaintenanceState.RECOVERY_REQUIRED)
        self.assertEqual(
            self.repository.get_assignment_state(11).workflow_state,
            MaintenanceWorkflowState.RECOVERY_REQUIRED,
        )
        self.assertTrue(self.repository.list_active_locks(plan.id))
        self.assertIsNotNone(self.connection.execute(
            "SELECT operation_run_id FROM cluster_assignments WHERE id=11",
        ).fetchone()["operation_run_id"])
        self.assertIsNone(self.connection.execute(
            "SELECT operation_run_id FROM cluster_assignments WHERE id=12",
        ).fetchone()["operation_run_id"])

    async def test_stateless_container_skips_allocation_guard(self):
        plan = self.plan(12)
        guard = FakeGuard()
        service = ContainerMaintenanceService(
            self.repository,
            execution=self.execution,
            target_resolver=lambda assignment_id: self.target(assignment_id),
            runtime=FakeRuntime(),
            companions=FakeCompanions(),
            allocation_guard=guard,
        )

        await service.prepare(plan.id, username="operator")

        self.assertEqual(guard.calls, [])

    async def test_stop_failure_enters_recovery_without_releasing_the_selected_claim(self):
        plan = self.plan()
        runtime = FakeRuntime(stop_confirmed=False)
        service = ContainerMaintenanceService(
            self.repository,
            execution=self.execution,
            target_resolver=lambda assignment_id: self.target(assignment_id),
            runtime=runtime,
            companions=FakeCompanions(),
            allocation_guard=FakeGuard(),
        )
        await service.prepare(plan.id, username="operator")

        with self.assertRaisesRegex(RuntimeError, "stop"):
            await service.stop(plan.id, username="operator")

        self.assertEqual(
            self.repository.get_assignment_state(11).workflow_state,
            MaintenanceWorkflowState.RECOVERY_REQUIRED,
        )
        self.assertEqual(self.repository.get_plan(plan.id).lifecycle_state, MaintenanceState.RECOVERY_REQUIRED)
        self.assertTrue(self.repository.list_active_locks(plan.id))
        self.assertIsNotNone(self.connection.execute(
            "SELECT operation_run_id FROM cluster_assignments WHERE id=11",
        ).fetchone()["operation_run_id"])
        self.assertIsNone(self.connection.execute(
            "SELECT operation_run_id FROM cluster_assignments WHERE id=12",
        ).fetchone()["operation_run_id"])

    async def test_wrong_runtime_target_identity_fails_before_a_run_or_lock_is_created(self):
        plan = self.plan()
        service = ContainerMaintenanceService(
            self.repository,
            execution=self.execution,
            target_resolver=lambda _assignment_id: self.target(12),
            runtime=FakeRuntime(),
            companions=FakeCompanions(),
            allocation_guard=FakeGuard(),
        )

        with self.assertRaisesRegex(RuntimeError, "identity"):
            await service.prepare(plan.id, username="operator")

        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 0)
        self.assertEqual(self.repository.list_active_locks(plan.id), [])
        self.assertEqual(self.repository.get_assignment_state(11).workflow_state, MaintenanceWorkflowState.AVAILABLE)

    async def test_failed_return_preserves_guard_and_claim_for_explicit_recovery(self):
        plan = self.plan()
        guard = FakeGuard()
        service = ContainerMaintenanceService(
            self.repository,
            execution=self.execution,
            target_resolver=lambda assignment_id: self.target(assignment_id),
            runtime=FakeRuntime(start_confirmed=False),
            companions=FakeCompanions(),
            allocation_guard=guard,
        )
        await service.prepare(plan.id, username="operator")
        await service.stop(plan.id, username="operator")

        with self.assertRaisesRegex(RuntimeError, "start"):
            await service.return_to_service(plan.id, username="operator")

        self.assertEqual(self.repository.get_plan(plan.id).lifecycle_state, MaintenanceState.RECOVERY_REQUIRED)
        self.assertEqual(self.repository.get_assignment_state(11).workflow_state, MaintenanceWorkflowState.RECOVERY_REQUIRED)
        self.assertTrue(self.repository.list_active_locks(plan.id))
        self.assertIsNotNone(self.connection.execute(
            "SELECT operation_run_id FROM cluster_assignments WHERE id=11",
        ).fetchone()["operation_run_id"])
        self.assertEqual(guard.calls, [("capture", plan.id, 1), ("activate", plan.id, 1)])


if __name__ == "__main__":
    unittest.main()
