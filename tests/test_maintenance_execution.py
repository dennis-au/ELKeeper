from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timedelta, timezone

from app.maintenance_execution import (
    AdapterResult,
    MaintenanceAction,
    MaintenanceExecutionService,
    MaintenanceValidationError,
)
from app.maintenance_lifecycle import MaintenanceState, SideEffectState
from app.maintenance_store import LockRequest, MaintenanceRepository, install_maintenance_schema


NOW = datetime(2026, 8, 3, 8, 0, tzinfo=timezone.utc)


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
        CREATE TABLE clusters (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL);
        CREATE TABLE memberships (
          cluster_id INTEGER NOT NULL REFERENCES clusters(id),
          node_id INTEGER NOT NULL REFERENCES nodes(id),
          PRIMARY KEY(cluster_id,node_id)
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
          revision INTEGER NOT NULL DEFAULT 1,
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
        INSERT INTO nodes(id,name) VALUES(1,'node-a');
        INSERT INTO clusters(id,name) VALUES(1,'cluster-a');
        INSERT INTO cluster_assignments(id,cluster_id,node_id,revision) VALUES(11,1,1,4);
    """)
    install_maintenance_schema(value)
    return value


class MaintenanceExecutionTests(unittest.TestCase):
    def setUp(self):
        self.connection = connection()
        self.repository = MaintenanceRepository(self.connection)
        self.service = MaintenanceExecutionService(
            self.repository,
            capability_revision=lambda: "cap-v1",
            clock=lambda: NOW,
        )

    def tearDown(self):
        self.connection.close()

    def plan(self, *, observed_at=NOW, expires_at=None, state="ready"):
        return self.repository.create_plan(
            operation_kind="reboot",
            plan={
                "policy": {"observation_max_age_seconds": 120},
                "observation": {
                    "captured_at": observed_at.isoformat().replace("+00:00", "Z"),
                    "capability_revision": "cap-v1",
                },
                "steps": [],
            },
            observation={
                "captured_at": observed_at.isoformat().replace("+00:00", "Z"),
                "capability_revision": "cap-v1",
            },
            idempotency_key=f"plan-{observed_at.timestamp()}-{state}",
            requested_by="operator",
            expires_at=expires_at or NOW + timedelta(minutes=5),
            target_node_id=1,
            target_manifest={
                "affected_cluster_ids": [1],
                "assignment_revisions": [{"assignment_id": 11, "revision": 4}],
                "policy_revisions": [{"cluster_id": 1, "revision": 0}],
            },
            initial_state=state,
        )

    def test_execute_preparation_attaches_one_run_and_acquires_all_scopes_atomically(self):
        plan = self.plan()

        ticket = self.service.prepare(plan.id, MaintenanceAction.EXECUTE, username="operator")

        current = self.repository.get_plan(plan.id)
        self.assertEqual(current.lifecycle_state, MaintenanceState.EXECUTING)
        self.assertEqual(current.run_id, ticket.run_id)
        run = self.connection.execute("SELECT * FROM runs WHERE id=?", (ticket.run_id,)).fetchone()
        self.assertEqual(run["status"], "running")
        self.assertNotIn(ticket.owner_token, run["context_json"])
        self.assertEqual(
            {(item.scope.value, item.identifier) for item in self.repository.list_active_locks(plan.id)},
            {("host", "1"), ("cluster", "1"), ("assignment", "11")},
        )

        response = self.service.finalize(ticket, AdapterResult())
        self.assertEqual(response.lifecycle_state, MaintenanceState.EXECUTING)
        self.assertEqual(len(self.repository.list_active_locks(plan.id)), 3)

    def test_validation_failures_create_no_run_lock_or_lifecycle_change(self):
        plan = self.plan(expires_at=NOW - timedelta(seconds=1))
        with self.assertRaises(MaintenanceValidationError):
            self.service.prepare(plan.id, MaintenanceAction.EXECUTE, username="operator")
        self.connection.execute("DELETE FROM maintenance_plans WHERE id=?", (plan.id,))

        plan = self.plan(observed_at=NOW - timedelta(seconds=121))
        with self.assertRaises(MaintenanceValidationError):
            self.service.prepare(plan.id, MaintenanceAction.EXECUTE, username="operator")
        self.connection.execute("DELETE FROM maintenance_plans WHERE id=?", (plan.id,))

        self.connection.execute("UPDATE cluster_assignments SET revision=5 WHERE id=11")
        changed_revision = self.plan(observed_at=NOW + timedelta(seconds=1))
        with self.assertRaises(MaintenanceValidationError):
            self.service.prepare(changed_revision.id, MaintenanceAction.EXECUTE, username="operator")

        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 0)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM maintenance_locks").fetchone()[0], 0)
        states = {
            row["lifecycle_state"]
            for row in self.connection.execute("SELECT lifecycle_state FROM maintenance_plans")
        }
        self.assertEqual(states, {"ready"})

    def test_hash_and_capability_revision_are_checked_before_execution(self):
        plan = self.plan()
        self.connection.execute("DROP TRIGGER maintenance_plan_immutable_content")
        self.connection.execute(
            "UPDATE maintenance_plans SET plan_json=json_set(plan_json,'$.policy.max_unavailable',2) WHERE id=?",
            (plan.id,),
        )
        with self.assertRaisesRegex(MaintenanceValidationError, "hash"):
            self.service.prepare(plan.id, MaintenanceAction.EXECUTE, username="operator")

        self.connection.execute("DELETE FROM maintenance_plans WHERE id=?", (plan.id,))
        other = self.plan(observed_at=NOW + timedelta(seconds=1))
        service = MaintenanceExecutionService(
            self.repository,
            capability_revision=lambda: "cap-v2",
            clock=lambda: NOW,
        )
        with self.assertRaisesRegex(MaintenanceValidationError, "capabilit"):
            service.prepare(other.id, MaintenanceAction.EXECUTE, username="operator")

    def test_pause_resume_and_cancel_are_limited_to_safe_checkpoints(self):
        plan = self.plan()
        execute = self.service.prepare(plan.id, MaintenanceAction.EXECUTE, username="operator")
        self.service.finalize(execute, AdapterResult())

        pause = self.service.prepare(plan.id, MaintenanceAction.PAUSE, username="operator")
        self.service.finalize(pause, AdapterResult())
        self.assertEqual(self.repository.get_plan(plan.id).lifecycle_state, MaintenanceState.PAUSED)

        resume = self.service.prepare(plan.id, MaintenanceAction.RESUME, username="operator")
        self.service.finalize(resume, AdapterResult())
        self.assertEqual(self.repository.get_plan(plan.id).lifecycle_state, MaintenanceState.EXECUTING)

        self.repository.record_checkpoint(
            plan_id=plan.id,
            checkpoint_key="reboot.intent",
            sequence=300,
            side_effect_state=SideEffectState.MAY_HAVE_STARTED,
            payload={"operation_id": "operation-a"},
        )
        with self.assertRaisesRegex(MaintenanceValidationError, "safe checkpoint"):
            self.service.prepare(plan.id, MaintenanceAction.PAUSE, username="operator")
        with self.assertRaisesRegex(MaintenanceValidationError, "safe checkpoint"):
            self.service.prepare(plan.id, MaintenanceAction.CANCEL, username="operator")

    def test_adapter_failure_enters_recovery_and_retains_locks(self):
        plan = self.plan()
        ticket = self.service.prepare(plan.id, MaintenanceAction.EXECUTE, username="operator")

        result = self.service.fail(ticket, error_category="adapter-failed")

        self.assertEqual(result.lifecycle_state, MaintenanceState.RECOVERY_REQUIRED)
        self.assertEqual(len(self.repository.list_active_locks(plan.id)), 3)
        run = self.connection.execute("SELECT status,log FROM runs WHERE id=?", (ticket.run_id,)).fetchone()
        self.assertEqual(run["status"], "recovery_required")
        self.assertNotIn("Traceback", run["log"])

    def test_cancel_releases_non_stale_locks_only_after_adapter_success(self):
        plan = self.plan()
        execute = self.service.prepare(plan.id, MaintenanceAction.EXECUTE, username="operator")
        self.service.finalize(execute, AdapterResult())

        cancel = self.service.prepare(plan.id, MaintenanceAction.CANCEL, username="operator")
        result = self.service.finalize(cancel, AdapterResult())

        self.assertEqual(result.lifecycle_state, MaintenanceState.CANCELLED)
        self.assertEqual(self.repository.list_active_locks(plan.id), [])
        run = self.connection.execute("SELECT status FROM runs WHERE id=?", (execute.run_id,)).fetchone()
        self.assertEqual(run["status"], "cancelled")

    def test_recovery_requires_evidence_before_releasing_an_expired_lock(self):
        plan = self.plan()
        execute = self.service.prepare(plan.id, MaintenanceAction.EXECUTE, username="operator")
        self.service.fail(execute, error_category="adapter-failed")
        lock = self.repository.list_active_locks(plan.id)[0]
        self.connection.execute(
            "UPDATE maintenance_locks SET expires_at=? WHERE owner_plan_id=?",
            ((NOW - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"), plan.id),
        )

        recover = self.service.prepare(plan.id, MaintenanceAction.RECOVER, username="operator")
        with self.assertRaisesRegex(MaintenanceValidationError, "rediscovery evidence"):
            self.service.finalize(recover, AdapterResult(lifecycle_state="executing"))

        evidence = {
            str(item.id): {"remote_state": "rediscovered", "observed_at": NOW.isoformat()}
            for item in self.repository.list_active_locks(plan.id)
        }
        recovered = self.service.finalize(
            recover,
            AdapterResult(lifecycle_state="executing", stale_lock_evidence=evidence),
        )
        self.assertEqual(recovered.lifecycle_state, MaintenanceState.EXECUTING)
        self.assertTrue(self.repository.list_active_locks(plan.id))
        old = self.repository.get_lock(lock.id)
        self.assertIsNotNone(old.stale_released_at)


if __name__ == "__main__":
    unittest.main()
