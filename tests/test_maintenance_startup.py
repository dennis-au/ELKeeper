from __future__ import annotations

import importlib
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from app.maintenance_store import MaintenanceRepository


class MaintenanceStartupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        os.environ.update(
            APP_DATA_DIR=self.temp.name,
            APP_RUNTIME_DIR=os.path.join(self.temp.name, "runtime"),
            APP_SECRET_KEY="test-secret",
            ADMIN_USERNAME="operator",
            ADMIN_PASSWORD="test-password",
        )
        import app.main

        self.main = importlib.reload(app.main)
        self.main.init()

    def tearDown(self):
        self.temp.cleanup()

    def test_restart_preserves_maintenance_runs_and_keeps_legacy_cleanup_behavior(self):
        with self.main.db() as connection:
            connection.execute(
                "INSERT INTO clusters(name,slug,ports_json) VALUES(?,?,?)",
                ("cluster-a", "cluster-a", json.dumps(self.main.DEFAULT_PORTS)),
            )
            cluster_id = connection.execute("SELECT id FROM clusters WHERE slug='cluster-a'").fetchone()["id"]
            maintenance_run = connection.execute(
                "INSERT INTO runs(kind,target,status,command_json) VALUES('maintenance','node-a','running','[]')"
            ).lastrowid
            legacy_batch_run = connection.execute(
                "INSERT INTO runs(kind,target,status,command_json) VALUES('workload-batch','cluster-a','running','[]')"
            ).lastrowid
            legacy_run = connection.execute(
                "INSERT INTO runs(kind,target,status,command_json) VALUES('probe','node-a','queued','[]')"
            ).lastrowid
            repository = MaintenanceRepository(connection)
            plan = repository.create_plan(
                operation_kind="reboot",
                plan={"steps": []},
                idempotency_key="startup-maintenance",
                requested_by="operator",
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
                run_id=maintenance_run,
                initial_state="ready",
            )
            repository.transition_plan(plan.id, plan.state_revision, "executing")
            connection.execute(
                "INSERT INTO workload_change_batches(run_id,cluster_id,plan_encrypted) VALUES(?,?,?)",
                (maintenance_run, cluster_id, "maintenance-plan"),
            )
            connection.execute(
                "INSERT INTO workload_change_batches(run_id,cluster_id,plan_encrypted) VALUES(?,?,?)",
                (legacy_batch_run, cluster_id, "legacy-plan"),
            )

        protected_inventory = self.main.INVENTORIES / f"run-{maintenance_run}.yaml"
        protected_variables = self.main.VARIABLES / f"run-{maintenance_run}-executor.yaml"
        legacy_inventory = self.main.INVENTORIES / f"run-{legacy_batch_run}.yaml"
        generic_variables = self.main.VARIABLES / f"run-{legacy_run}-probe.yaml"
        for path in (protected_inventory, protected_variables, legacy_inventory, generic_variables):
            path.write_text("redacted: true\n", encoding="utf-8")

        self.main.init()

        with self.main.db() as connection:
            statuses = {
                row["id"]: row["status"]
                for row in connection.execute(
                    "SELECT id,status FROM runs WHERE id IN (?,?,?)",
                    (maintenance_run, legacy_batch_run, legacy_run),
                )
            }
            phases = {
                row["run_id"]: row["phase"]
                for row in connection.execute(
                    "SELECT run_id,phase FROM workload_change_batches WHERE run_id IN (?,?)",
                    (maintenance_run, legacy_batch_run),
                )
            }
            stored_plan = MaintenanceRepository(connection).get_plan(plan.id)
            first_log = connection.execute("SELECT log FROM runs WHERE id=?", (maintenance_run,)).fetchone()["log"]

        self.assertEqual(statuses[maintenance_run], "recovery_required")
        self.assertEqual(statuses[legacy_batch_run], "recovery_required")
        self.assertEqual(statuses[legacy_run], "failed")
        self.assertEqual(stored_plan.lifecycle_state.value, "recovery_required")
        self.assertEqual(phases[maintenance_run], "applying")
        self.assertEqual(phases[legacy_batch_run], "rolling_back")
        self.assertTrue(protected_inventory.exists())
        self.assertTrue(protected_variables.exists())
        self.assertFalse(legacy_inventory.exists())
        self.assertFalse(generic_variables.exists())

        self.main.init()
        with self.main.db() as connection:
            second_log = connection.execute("SELECT log FROM runs WHERE id=?", (maintenance_run,)).fetchone()["log"]
        self.assertEqual(second_log, first_log)


if __name__ == "__main__":
    unittest.main()
