from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timedelta, timezone

from app.maintenance_lifecycle import MaintenanceState, MaintenanceStepState, SideEffectState
from app.maintenance_status import MaintenanceActionCapabilities, serialize_maintenance_operation
from app.modules.maintenance.planned_contracts import MaintenanceWorkflowState
from app.modules.maintenance.store import MaintenanceRepository, install_maintenance_schema


NOW = datetime(2026, 8, 3, tzinfo=timezone.utc)


class MaintenanceStatusTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript("""
            CREATE TABLE users (username TEXT PRIMARY KEY, password_hash TEXT NOT NULL);
            CREATE TABLE nodes (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE clusters (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
            CREATE TABLE runs (id INTEGER PRIMARY KEY, status TEXT NOT NULL, kind TEXT NOT NULL DEFAULT '', target TEXT NOT NULL DEFAULT '', context_json TEXT NOT NULL DEFAULT '{}', log TEXT NOT NULL DEFAULT '', finished_at TEXT);
            CREATE TABLE cluster_assignments (id INTEGER PRIMARY KEY, cluster_id INTEGER NOT NULL REFERENCES clusters(id), node_id INTEGER NOT NULL REFERENCES nodes(id), revision INTEGER NOT NULL DEFAULT 1, operation_run_id INTEGER REFERENCES runs(id));
            CREATE TABLE memberships (cluster_id INTEGER NOT NULL REFERENCES clusters(id), node_id INTEGER NOT NULL REFERENCES nodes(id), PRIMARY KEY(cluster_id,node_id));
            CREATE TABLE host_runtime_observations (node_id INTEGER PRIMARY KEY REFERENCES nodes(id), initialized INTEGER NOT NULL DEFAULT 0, reachable INTEGER NOT NULL DEFAULT 0, podman_socket_active INTEGER NOT NULL DEFAULT 0, os_name TEXT NOT NULL DEFAULT '', podman_version TEXT NOT NULL DEFAULT '', observed_at TEXT, last_error TEXT NOT NULL DEFAULT '');
            CREATE TABLE workload_change_batches (run_id INTEGER PRIMARY KEY REFERENCES runs(id), cluster_id INTEGER NOT NULL REFERENCES clusters(id), plan_encrypted TEXT NOT NULL, completed_json TEXT NOT NULL DEFAULT '[]', phase TEXT NOT NULL DEFAULT 'applying');
            CREATE TABLE audit_events (id INTEGER PRIMARY KEY, username TEXT NOT NULL, action TEXT NOT NULL, cluster_id INTEGER, item_id TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            INSERT INTO nodes(id,name) VALUES(1,'node-a');
        """)
        install_maintenance_schema(self.connection)
        self.repository = MaintenanceRepository(self.connection)
        self.plan = self.repository.create_plan(
            operation_kind="reboot",
            plan={"steps": [{"kind": "reboot-host"}]},
            idempotency_key="status-test",
            requested_by="operator",
            expires_at=NOW + timedelta(minutes=5),
            target_node_id=1,
            initial_state=MaintenanceState.READY,
        )
        self.step = self.repository.create_step(
            plan_id=self.plan.id,
            step_key="preview:1:reboot-host",
            sequence=1,
            step_kind="reboot-host",
            affected_node_id=1,
        )

    def tearDown(self):
        self.connection.close()

    def status(self, **kwargs):
        plan = self.repository.get_plan(self.plan.id)
        return serialize_maintenance_operation(
            plan,
            steps=self.repository.list_steps(plan.id),
            checkpoints=self.repository.list_checkpoints(plan.id),
            **kwargs,
        )

    def execute_plan(self):
        self.plan = self.repository.transition_plan(self.plan.id, self.plan.state_revision, MaintenanceState.EXECUTING)

    def test_ready_plan_has_no_actions_without_explicit_capabilities(self):
        payload = self.status()
        self.assertTrue(payload["safe_checkpoint"])
        self.assertEqual(payload["action_controls"], {})
        self.assertEqual(payload["progress"]["hostBoot"]["state"], "not_started")

    def test_safe_cluster_preparation_checkpoint_authorizes_server_controls(self):
        self.execute_plan()
        self.repository.record_checkpoint(
            plan_id=self.plan.id,
            checkpoint_key="reboot.clusters-prepared",
            sequence=1,
            side_effect_state=SideEffectState.PREPARED,
            payload={"cluster_ids": []},
        )
        payload = self.status(capabilities=MaintenanceActionCapabilities(pause=True, cancel=True))
        self.assertTrue(payload["safe_checkpoint"])
        self.assertTrue(payload["action_controls"]["pause"]["enabled"])
        self.assertTrue(payload["action_controls"]["cancel"]["enabled"])

    def test_ambiguous_reboot_checkpoint_fails_closed_but_allows_observational_recovery(self):
        self.execute_plan()
        self.repository.record_checkpoint(
            plan_id=self.plan.id,
            checkpoint_key="reboot.intent",
            sequence=1,
            side_effect_state=SideEffectState.MAY_HAVE_STARTED,
            payload={"operation_id": "a" * 32},
        )
        self.plan = self.repository.transition_plan(self.plan.id, self.plan.state_revision, MaintenanceState.RECOVERY_REQUIRED)
        payload = self.status(capabilities=MaintenanceActionCapabilities(cancel=True, recover=True))
        self.assertFalse(payload["safe_checkpoint"])
        self.assertFalse(payload["action_controls"]["cancel"]["enabled"])
        self.assertTrue(payload["action_controls"]["recover"]["enabled"])
        self.assertFalse(payload["action_controls"]["recover"]["requiresSafeCheckpoint"])

    def test_executor_contract_distinguishes_manifest_signature_from_result_identity(self):
        payload = self.status(evidence={
            "executor": {
                "state": "complete",
                "manifest_signature_verified": True,
                "result_identity_verified": True,
                "result_imported": True,
            },
        })
        executor = payload["progress"]["executor"]
        self.assertTrue(executor["signatureVerified"])
        self.assertTrue(executor["resultIdentityVerified"])
        self.assertNotIn("resultSignatureVerified", executor)

    def test_unresolved_cleanup_blocks_cancel_and_redacts_evidence(self):
        payload = self.status(
            capabilities=MaintenanceActionCapabilities(cancel=True),
            evidence={
                "cleanup": [{"id": "allocation-a", "kind": "allocation", "clusterName": "a", "state": "unresolved"}],
                "executor": {"reason": "password should not persist", "password": "secret"},
            },
        )
        self.assertFalse(payload["action_controls"]["cancel"]["enabled"])
        self.assertEqual(payload["progress"]["executor"].get("password"), None)

    def test_verified_steps_drive_progress_and_terminal_plans_expose_no_actions(self):
        active = self.repository.transition_step(self.step.id, self.step.state_revision, MaintenanceStepState.EXECUTING)
        self.repository.transition_step(active.id, active.state_revision, MaintenanceStepState.VERIFIED)
        self.plan = self.repository.transition_plan(self.plan.id, self.plan.state_revision, MaintenanceState.EXECUTING)
        self.plan = self.repository.transition_plan(self.plan.id, self.plan.state_revision, MaintenanceState.SUCCEEDED)
        payload = self.status(capabilities=MaintenanceActionCapabilities(pause=True, resume=True, cancel=True, recover=True))
        self.assertEqual(payload["progress"]["progress"], {"completed": 1, "total": 1})
        self.assertEqual(payload["action_controls"], {})

    def test_persisted_workflow_state_is_available_to_the_operator_read_model(self):
        payload = self.status(
            workflow_state=MaintenanceWorkflowState.READY_TO_STOP,
            workflow_scope="host_maintenance",
        )

        self.assertEqual(payload["progress"]["workflowState"], "ready_to_stop")
        self.assertEqual(payload["progress"]["workflowScope"], "host_maintenance")


if __name__ == "__main__":
    unittest.main()
