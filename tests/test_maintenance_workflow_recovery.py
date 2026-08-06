from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timedelta, timezone

from app.modules.maintenance.execution import MaintenanceAction, MaintenanceExecutionService
from app.modules.maintenance.lifecycle import HostMaintenanceState, MaintenanceState, SideEffectState
from app.modules.maintenance.planned_contracts import MaintenanceWorkflowState
from app.modules.maintenance.recovery import StartupRecoveryClassification
from app.modules.maintenance.store import MaintenanceRepository, install_maintenance_schema
from app.modules.maintenance.workflow_recovery import (
    MaintenanceWorkflowRecoveryService,
    RebootRecoveryDisposition,
)


NOW = datetime(2026, 8, 5, 10, 0, tzinfo=timezone.utc)


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
        INSERT INTO clusters(id,name,slug) VALUES(1,'cluster-a','alpha');
        INSERT INTO memberships(cluster_id,node_id) VALUES(1,1);
        INSERT INTO cluster_assignments(id,cluster_id,node_id,role,revision,state) VALUES
          (11,1,1,'hot',4,'active'),
          (12,1,1,'kibana',2,'active');
    """)
    install_maintenance_schema(value)
    return value


class MaintenanceWorkflowRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.connection = connection()
        self.repository = MaintenanceRepository(self.connection)
        self.execution = MaintenanceExecutionService(
            self.repository,
            capability_revision=lambda: "cap-v1",
            clock=lambda: NOW,
        )
        self.recovery = MaintenanceWorkflowRecoveryService(self.repository)
        self.addCleanup(self.connection.close)

    def active_host_plan(self):
        plan = self.repository.create_plan(
            operation_kind="reboot",
            plan={"policy": {"observation_max_age_seconds": 120}},
            observation={
                "captured_at": NOW.isoformat().replace("+00:00", "Z"),
                "capability_revision": "cap-v1",
            },
            idempotency_key="host-maintenance-recovery",
            requested_by="operator",
            expires_at=NOW + timedelta(seconds=60),
            target_node_id=1,
            target_manifest={
                "public_operation": "host_maintenance",
                "affected_cluster_ids": [1],
                "assignment_revisions": [
                    {"assignment_id": 11, "revision": 4},
                    {"assignment_id": 12, "revision": 2},
                ],
            },
            initial_state=MaintenanceState.READY,
        )
        self.execution.prepare(plan.id, MaintenanceAction.EXECUTE, username="operator")
        host = self.repository.get_host_state(1)
        host = self.repository.transition_host_state(1, host.state_revision, HostMaintenanceState.PLANNING, plan.id)
        self.repository.transition_host_workflow_state(
            1,
            host.workflow_state_revision,
            MaintenanceWorkflowState.READY_TO_STOP,
        )
        for assignment_id in (11, 12):
            state = self.repository.get_assignment_state(assignment_id)
            state = self.repository.transition_assignment_state(
                assignment_id,
                state.state_revision,
                MaintenanceWorkflowState.PREPARING,
                plan.id,
            )
            self.repository.transition_assignment_state(
                assignment_id,
                state.state_revision,
                MaintenanceWorkflowState.READY_TO_STOP,
                plan.id,
            )
        return self.repository.get_plan(plan.id)

    def test_expiry_preserves_exact_workflow_ownership_for_recovery(self):
        plan = self.active_host_plan()

        results = self.recovery.expire_due_workflows(
            now=NOW + timedelta(seconds=61),
            username="operator",
        )

        self.assertEqual([item.plan_id for item in results], [plan.id])
        self.assertEqual(self.repository.get_plan(plan.id).lifecycle_state, MaintenanceState.RECOVERY_REQUIRED)
        self.assertEqual(self.repository.get_host_state(1).state, HostMaintenanceState.RECOVERY_REQUIRED)
        self.assertEqual(
            [self.repository.get_assignment_state(assignment_id).workflow_state for assignment_id in (11, 12)],
            [MaintenanceWorkflowState.RECOVERY_REQUIRED, MaintenanceWorkflowState.RECOVERY_REQUIRED],
        )
        self.assertTrue(self.repository.list_active_locks(plan.id))
        run_id = self.repository.get_plan(plan.id).run_id
        self.assertEqual(self.connection.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()["status"], "recovery_required")

        repeated = self.recovery.expire_due_workflows(
            now=NOW + timedelta(seconds=62),
            username="operator",
        )
        self.assertEqual([item.plan_id for item in repeated], [plan.id])
        self.assertTrue(self.repository.list_active_locks(plan.id))

    def test_startup_reconciliation_marks_host_and_assignments_recovery_required(self):
        plan = self.active_host_plan()

        startup = self.repository.prepare_startup_recovery()
        results = self.recovery.reconcile_startup_workflows(username="system")

        self.assertIn(plan.id, startup.transitioned_plan_ids)
        self.assertEqual([item.plan_id for item in results], [plan.id])
        self.assertEqual(self.repository.get_host_state(1).workflow_state, MaintenanceWorkflowState.RECOVERY_REQUIRED)
        self.assertEqual(
            [self.repository.get_assignment_state(assignment_id).workflow_state for assignment_id in (11, 12)],
            [MaintenanceWorkflowState.RECOVERY_REQUIRED, MaintenanceWorkflowState.RECOVERY_REQUIRED],
        )
        self.assertTrue(self.repository.list_active_locks(plan.id))

    def test_missing_manifest_assignment_is_retained_as_recovery_evidence(self):
        plan = self.repository.create_plan(
            operation_kind="reboot",
            plan={"policy": {"observation_max_age_seconds": 120}},
            observation={"captured_at": NOW.isoformat().replace("+00:00", "Z")},
            idempotency_key="missing-workflow-target",
            requested_by="operator",
            expires_at=NOW + timedelta(minutes=5),
            target_node_id=1,
            target_manifest={
                "public_operation": "host_maintenance",
                "assignment_revisions": [{"assignment_id": 99, "revision": 1}],
            },
            initial_state=MaintenanceState.READY,
        )
        plan = self.repository.transition_plan(plan.id, plan.state_revision, MaintenanceState.EXECUTING)

        result = self.recovery.reconcile_plan(plan.id, reason="missing-target", username="operator")

        self.assertEqual(result.missing_assignment_ids, (99,))
        self.assertEqual(self.repository.get_plan(plan.id).lifecycle_state, MaintenanceState.RECOVERY_REQUIRED)
        audit = self.connection.execute(
            "SELECT action,detail FROM audit_events WHERE item_id=? ORDER BY id DESC LIMIT 1",
            (plan.id,),
        ).fetchone()
        self.assertEqual(audit["action"], "maintenance-workflow-recovery-required")
        self.assertIn("99", audit["detail"])

    def test_restart_before_reboot_intent_is_classified_as_resumable_without_remote_cleanup(self):
        plan = self.active_host_plan()
        self._record_reboot_checkpoint(
            plan.id,
            "host-reboot-request",
            4900,
            SideEffectState.PREPARED,
        )

        startup = self.repository.prepare_startup_recovery()
        result = self.recovery.reconcile_startup_workflows(username="system")[0]

        self.assertEqual(startup.classifications[0].classification, StartupRecoveryClassification.INCOMPLETE)
        self.assertEqual(startup.classifications[0].reason_code, "reboot-not-invoked")
        self.assertTrue(startup.classifications[0].resumable)
        self.assertEqual(result.reboot_disposition, RebootRecoveryDisposition.RESUME_PRE_REBOOT)
        self.assertEqual(result.reboot_checkpoint, "host-reboot-request")
        self.assertTrue(result.resume_allowed)
        self.assertFalse(result.observation_required)
        self.assertEqual(self.repository.get_plan(plan.id).lifecycle_state, MaintenanceState.RECOVERY_REQUIRED)
        self.assertNotIn(
            "host:executor-cleanup:host",
            [item.checkpoint_key for item in self.repository.list_checkpoints(plan.id)],
        )

    def test_restart_after_reboot_intent_without_acknowledgement_requires_observation(self):
        plan = self.active_host_plan()
        self._record_reboot_checkpoint(
            plan.id,
            "reboot.intent",
            5700,
            SideEffectState.PREPARED,
        )

        startup = self.repository.prepare_startup_recovery()
        result = self.recovery.reconcile_startup_workflows(username="system")[0]

        self.assertEqual(startup.classifications[0].classification, StartupRecoveryClassification.AMBIGUOUS)
        self.assertEqual(startup.classifications[0].reason_code, "reboot-outcome-requires-rediscovery")
        self.assertFalse(startup.classifications[0].resumable)
        self.assertEqual(result.reboot_disposition, RebootRecoveryDisposition.OBSERVE_REBOOT)
        self.assertEqual(result.reboot_checkpoint, "reboot.intent")
        self.assertFalse(result.resume_allowed)
        self.assertTrue(result.observation_required)

    def test_restart_after_acknowledgement_or_reconnect_stays_ambiguous_until_return_is_verified(self):
        for checkpoint_key, sequence, state in (
            ("reboot.invocation-acknowledged", 5800, SideEffectState.MAY_HAVE_STARTED),
            ("reboot.ssh-disconnected", 5900, SideEffectState.MAY_HAVE_STARTED),
            ("reboot.host-reconnected", 6000, SideEffectState.VERIFIED),
        ):
            with self.subTest(checkpoint=checkpoint_key):
                self._reset_context()
                plan = self.active_host_plan()
                self._record_reboot_checkpoint(plan.id, "reboot.intent", 5700, SideEffectState.PREPARED)
                self._record_reboot_checkpoint(plan.id, checkpoint_key, sequence, state)

                startup = self.repository.prepare_startup_recovery()
                result = self.recovery.reconcile_startup_workflows(username="system")[0]

                self.assertEqual(startup.classifications[0].classification, StartupRecoveryClassification.AMBIGUOUS)
                self.assertEqual(startup.classifications[0].reason_code, "reboot-outcome-requires-rediscovery")
                self.assertEqual(result.reboot_disposition, RebootRecoveryDisposition.OBSERVE_REBOOT)
                self.assertEqual(result.reboot_checkpoint, checkpoint_key)
                self.assertFalse(result.resume_allowed)
                self.assertTrue(result.observation_required)

    def test_restart_after_verified_reboot_return_can_only_continue_post_return(self):
        plan = self.active_host_plan()
        self._record_reboot_checkpoint(plan.id, "reboot.intent", 5700, SideEffectState.PREPARED)
        self._record_reboot_checkpoint(
            plan.id,
            "reboot.invocation-acknowledged",
            5800,
            SideEffectState.MAY_HAVE_STARTED,
        )
        self._record_reboot_checkpoint(
            plan.id,
            "reboot.return-discovered",
            6100,
            SideEffectState.VERIFIED,
        )

        startup = self.repository.prepare_startup_recovery()
        result = self.recovery.reconcile_startup_workflows(username="system")[0]

        self.assertEqual(startup.classifications[0].classification, StartupRecoveryClassification.INCOMPLETE)
        self.assertEqual(
            startup.classifications[0].reason_code,
            "reboot-return-verified-continue-post-return",
        )
        self.assertTrue(startup.classifications[0].resumable)
        self.assertEqual(result.reboot_disposition, RebootRecoveryDisposition.CONTINUE_POST_RETURN)
        self.assertEqual(result.reboot_checkpoint, "reboot.return-discovered")
        self.assertFalse(result.resume_allowed)
        self.assertFalse(result.observation_required)
        self.assertNotIn(
            "host:executor-cleanup:host",
            [item.checkpoint_key for item in self.repository.list_checkpoints(plan.id)],
        )

    def _record_reboot_checkpoint(
        self,
        plan_id: str,
        checkpoint_key: str,
        sequence: int,
        side_effect_state: SideEffectState,
    ) -> None:
        self.repository.record_checkpoint(
            plan_id=plan_id,
            checkpoint_key=checkpoint_key,
            sequence=sequence,
            side_effect_state=side_effect_state,
            payload={"operation_id": plan_id},
        )

    def _reset_context(self) -> None:
        self.connection.close()
        self.connection = connection()
        self.addCleanup(self.connection.close)
        self.repository = MaintenanceRepository(self.connection)
        self.execution = MaintenanceExecutionService(
            self.repository,
            capability_revision=lambda: "cap-v1",
            clock=lambda: NOW,
        )
        self.recovery = MaintenanceWorkflowRecoveryService(self.repository)


if __name__ == "__main__":
    unittest.main()
