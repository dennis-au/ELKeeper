from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.modules.maintenance.container_maintenance import ManagedContainerTarget
from app.modules.maintenance.host_reboot import HostMaintenanceRebootCoordinator
from app.modules.maintenance.lifecycle import MaintenanceState, SideEffectState
from app.modules.maintenance.reboot import RebootOrchestrationStatus
from app.modules.maintenance.store import MaintenanceRepository, install_maintenance_schema


NOW = datetime(2026, 8, 5, 10, 0, tzinfo=timezone.utc)
BOOT_ID = "01234567-89ab-cdef-0123-456789abcdef"


def connection() -> sqlite3.Connection:
    value = sqlite3.connect(":memory:")
    value.row_factory = sqlite3.Row
    value.execute("PRAGMA foreign_keys = ON")
    value.executescript("""
        CREATE TABLE nodes (id INTEGER PRIMARY KEY, name TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE clusters (id INTEGER PRIMARY KEY, name TEXT NOT NULL, slug TEXT NOT NULL);
        CREATE TABLE memberships (cluster_id INTEGER NOT NULL, node_id INTEGER NOT NULL);
        CREATE TABLE runs (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, target TEXT NOT NULL, status TEXT NOT NULL, command_json TEXT NOT NULL, log TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, finished_at TEXT, context_json TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE cluster_assignments (id INTEGER PRIMARY KEY, cluster_id INTEGER NOT NULL, node_id INTEGER NOT NULL, role TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, state TEXT NOT NULL DEFAULT 'active', operation_run_id INTEGER);
        CREATE TABLE workload_change_batches (run_id INTEGER PRIMARY KEY, cluster_id INTEGER NOT NULL);
        CREATE TABLE audit_events (id INTEGER PRIMARY KEY, username TEXT NOT NULL, action TEXT NOT NULL, cluster_id INTEGER, item_id TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        INSERT INTO nodes(id,name,enabled) VALUES(1,'node-a',1);
        INSERT INTO clusters(id,name,slug) VALUES(1,'cluster-a','alpha');
        INSERT INTO memberships(cluster_id,node_id) VALUES(1,1);
    """)
    install_maintenance_schema(value)
    return value


class FakeRuntime:
    def __init__(self):
        self.cleanup_targets = []

    async def read_boot_id(self, node_id: int):
        self.node_id = node_id
        return BOOT_ID

    async def cleanup_executor(self, target):
        self.cleanup_targets.append(target)
        return SimpleNamespace(proven=True)


class FakeOrchestrator:
    def __init__(self):
        self.requests = []

    async def run(self, request):
        self.requests.append(request)
        return SimpleNamespace(
            status=RebootOrchestrationStatus.READY_FOR_POST_RETURN,
            reason_code="return-discovered",
        )


class HostMaintenanceRebootCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.connection = connection()
        self.repository = MaintenanceRepository(self.connection)
        self.addCleanup(self.connection.close)
        self.plan = self.repository.create_plan(
            operation_kind="reboot",
            plan={"policy": {}},
            observation={"captured_at": NOW.isoformat().replace("+00:00", "Z")},
            idempotency_key="host-reboot-coordinator",
            requested_by="operator",
            expires_at=NOW + timedelta(minutes=30),
            target_node_id=1,
            target_manifest={"public_operation": "host_maintenance"},
            initial_state=MaintenanceState.READY,
        )
        self.runtime = FakeRuntime()
        self.orchestrator = FakeOrchestrator()
        self.coordinator = HostMaintenanceRebootCoordinator(
            self.repository,
            runtime=self.runtime,
            orchestrator=self.orchestrator,
            signing_key=Ed25519PrivateKey.from_private_bytes(b"m" * 32),
            clock=lambda: NOW,
        )
        self.targets = (
            ManagedContainerTarget(11, 1, 1, "master", "ecp-alpha-master-1.service", False),
            ManagedContainerTarget(12, 1, 1, "hot", "ecp-alpha-hot-1.service", True),
        )

    async def test_reboot_persists_a_signed_exact_target_request_before_dispatch(self):
        result = await self.coordinator.reboot(plan=self.plan, targets=self.targets)

        self.assertTrue(result.confirmed)
        request = self.orchestrator.requests[0]
        self.assertEqual(request.plan_id, self.plan.id)
        self.assertEqual(request.node_id, 1)
        self.assertEqual(request.pre_reboot_boot_id, BOOT_ID)
        self.assertEqual(
            request.executor_manifest.manifest.required_units,
            ("ecp-alpha-hot-1.service", "ecp-alpha-master-1.service"),
        )
        checkpoint = next(
            item for item in self.repository.list_checkpoints(self.plan.id)
            if item.checkpoint_key == "host-reboot-request"
        )
        self.assertEqual(checkpoint.side_effect_state, SideEffectState.PREPARED)

    async def test_cleanup_is_refused_until_the_reboot_return_checkpoint_exists(self):
        await self.coordinator.reboot(plan=self.plan, targets=self.targets)
        with self.assertRaisesRegex(RuntimeError, "return was not verified"):
            await self.coordinator.cleanup(plan=self.plan)

        self.repository.record_checkpoint(
            plan_id=self.plan.id,
            checkpoint_key="reboot.return-discovered",
            sequence=6100,
            side_effect_state=SideEffectState.VERIFIED,
            payload={"operation_id": self.plan.id},
        )
        result = await self.coordinator.cleanup(plan=self.plan)

        self.assertTrue(result.confirmed)
        target = self.runtime.cleanup_targets[0]
        self.assertEqual(target.operation_id, self.plan.id)
        self.assertEqual(target.unit, f"ecp-maintenance-resume@{self.plan.id}.service")


if __name__ == "__main__":
    unittest.main()
