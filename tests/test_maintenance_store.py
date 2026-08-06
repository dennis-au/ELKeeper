from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.maintenance_lifecycle import (
    PLAN_TRANSITIONS,
    WORKFLOW_TRANSITIONS,
    LockScope,
    MaintenanceState,
    MaintenanceStepState,
    MaintenanceWorkflowState,
    PlanHashInput,
    SideEffectState,
    TransitionError,
    canonical_plan_hash,
    derive_idempotency_key,
    validate_plan_transition,
    validate_workflow_transition,
)
from app.maintenance_recovery import RecoveryClassification, RecoveryEvidence, classify_recovery
from app.modules.maintenance.recovery import (
    MaintenanceStartupRecoveryCoordinator,
    RecoveryProjectionBundle,
    RecoveryProjectionResult,
    StartupRecoveryClassification,
)
from app.modules.maintenance.store import (
    IdempotencyConflict,
    LockConflict,
    LockRequest,
    MAINTENANCE_MIGRATIONS,
    MaintenanceRepository,
    MigrationDriftError,
    OverlappingPlanError,
    RevisionConflict,
    SCHEMA_VERSION,
    StaleLockRequiresRecovery,
    install_maintenance_schema,
)
from app.maintenance_planning import canonical_hash as compiled_plan_hash


NOW = datetime(2026, 8, 3, 0, 0, tzinfo=timezone.utc)


def base_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript("""
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
        INSERT INTO nodes(id,name) VALUES(1,'node-a'),(2,'node-b');
        INSERT INTO clusters(id,name) VALUES(1,'cluster-a'),(2,'cluster-b');
        INSERT INTO runs(id,status) VALUES(1,'queued'),(2,'running');
        INSERT INTO cluster_assignments(id,cluster_id,node_id,revision) VALUES(1,1,1,4),(2,2,1,2);
        INSERT INTO memberships(cluster_id,node_id) VALUES(1,1);
    """)
    return connection


class MaintenanceMigrationTests(unittest.TestCase):
    def test_install_is_additive_and_idempotent(self):
        connection = base_connection()
        self.addCleanup(connection.close)
        self.assertEqual(install_maintenance_schema(connection), SCHEMA_VERSION)
        self.assertEqual(install_maintenance_schema(connection), SCHEMA_VERSION)
        migration = connection.execute("SELECT * FROM maintenance_schema_migrations").fetchall()
        self.assertEqual(len(migration), SCHEMA_VERSION)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM clusters").fetchone()[0], 2)
        for table in (
            "maintenance_policies", "maintenance_plans", "maintenance_steps",
            "maintenance_checkpoints", "host_maintenance_state", "maintenance_locks",
            "maintenance_allocation_guards", "assignment_maintenance_state",
        ):
            self.assertIsNotNone(connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,),
            ).fetchone())

    def test_provider_ownership_migration_preserves_clusters_and_defaults_existing_rows(self):
        connection = base_connection()
        self.addCleanup(connection.close)

        install_maintenance_schema(connection)

        columns = {item["name"] for item in connection.execute("PRAGMA table_info(clusters)")}
        self.assertTrue({
            "provider_type", "ownership_state", "maintenance_backend",
            "provider_capabilities_json", "provider_connection_json", "expected_cluster_uuid",
            "provider_revision",
        }.issubset(columns))
        records = connection.execute(
            "SELECT provider_type,ownership_state,maintenance_backend,provider_capabilities_json,"
            "provider_connection_json,provider_revision FROM clusters ORDER BY id"
        ).fetchall()
        self.assertEqual(len(records), 2)
        for record in records:
            self.assertEqual(record["provider_type"], "native_podman")
            self.assertEqual(record["ownership_state"], "verified")
            self.assertEqual(record["maintenance_backend"], "documented_rolling")
            self.assertEqual(record["provider_capabilities_json"], "{}")
            self.assertEqual(record["provider_connection_json"], "{}")
            self.assertEqual(record["provider_revision"], 1)
        runtime_columns = {
            item["name"] for item in connection.execute("PRAGMA table_info(host_runtime_observations)")
        }
        self.assertIn("network_interfaces_json", runtime_columns)

    def test_workflow_state_migration_preserves_legacy_host_state(self):
        connection = base_connection()
        self.addCleanup(connection.close)
        legacy_migrations = MAINTENANCE_MIGRATIONS[:3]
        connection.execute("""
            CREATE TABLE maintenance_schema_migrations(
              version INTEGER PRIMARY KEY,name TEXT NOT NULL,checksum TEXT NOT NULL,applied_at TEXT NOT NULL
            )
        """)
        for version, name, checksum, _apply in legacy_migrations:
            connection.execute(
                "INSERT INTO maintenance_schema_migrations VALUES(?,?,?,?)",
                (version, name, checksum, "2026-08-04T00:00:00Z"),
            )
        connection.execute("""
            CREATE TABLE host_maintenance_state(
              node_id INTEGER PRIMARY KEY,state TEXT NOT NULL,active_plan_id TEXT,
              state_revision INTEGER NOT NULL,entered_at TEXT NOT NULL,updated_at TEXT NOT NULL
            )
        """)
        for node_id, state in ((1, "available"), (2, "maintenance")):
            connection.execute(
                "INSERT INTO host_maintenance_state VALUES(?,?,?,?,?,?)",
                (node_id, state, None, 1, "2026-08-04T00:00:00Z", "2026-08-04T00:00:00Z"),
            )

        self.assertEqual(install_maintenance_schema(connection), SCHEMA_VERSION)
        rows = connection.execute(
            "SELECT node_id,state,workflow_state,workflow_state_revision FROM host_maintenance_state ORDER BY node_id"
        ).fetchall()
        self.assertEqual(
            [tuple(row) for row in rows],
            [(1, "available", "available", 1), (2, "maintenance", "maintenance", 1)],
        )

    def test_partial_schema_is_completed_without_deleting_rows(self):
        connection = base_connection()
        self.addCleanup(connection.close)
        connection.execute("CREATE TABLE maintenance_policies(cluster_id INTEGER PRIMARY KEY, policy_json TEXT)")
        connection.execute(
            "CREATE TABLE maintenance_checkpoints("
            "id INTEGER PRIMARY KEY,plan_id TEXT,checkpoint_key TEXT,sequence INTEGER,"
            "side_effect_state TEXT,payload_json TEXT,observation_json TEXT,created_at TEXT)"
        )
        connection.execute("INSERT INTO maintenance_policies(cluster_id,policy_json) VALUES(1,'{\"window\":\"night\"}')")
        install_maintenance_schema(connection)
        row = connection.execute("SELECT * FROM maintenance_policies WHERE cluster_id=1").fetchone()
        self.assertEqual(row["policy_json"], '{"window":"night"}')
        self.assertIn("revision", {item["name"] for item in connection.execute("PRAGMA table_info(maintenance_policies)")})
        checkpoint_columns = {
            item["name"] for item in connection.execute("PRAGMA table_info(maintenance_checkpoints)")
        }
        self.assertIn("recovery_evidence_json", checkpoint_columns)

    def test_ledger_marked_legacy_interruption_is_reconciled_without_rewriting_history(self):
        connection = base_connection()
        self.addCleanup(connection.close)
        version, name, checksum, _apply = MAINTENANCE_MIGRATIONS[0]
        connection.executescript("""
            CREATE TABLE maintenance_schema_migrations(
              version INTEGER PRIMARY KEY,name TEXT NOT NULL,checksum TEXT NOT NULL,applied_at TEXT NOT NULL
            );
            CREATE TABLE maintenance_plans(id TEXT PRIMARY KEY, plan_json TEXT);
        """)
        connection.execute(
            "INSERT INTO maintenance_schema_migrations VALUES(?,?,?,?)",
            (version, name, checksum, "2026-08-03T00:00:00Z"),
        )
        connection.execute(
            "INSERT INTO maintenance_plans(id,plan_json) VALUES('legacy-interrupted','{\"step\":\"prepare\"}')"
        )

        self.assertEqual(install_maintenance_schema(connection), SCHEMA_VERSION)

        plan_columns = {item["name"] for item in connection.execute("PRAGMA table_info(maintenance_plans)")}
        self.assertTrue({"target_manifest_json", "lifecycle_state", "state_revision"}.issubset(plan_columns))
        self.assertEqual(
            connection.execute("SELECT plan_json FROM maintenance_plans WHERE id='legacy-interrupted'").fetchone()[0],
            '{"step":"prepare"}',
        )
        self.assertIsNotNone(connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='ux_maintenance_plans_idempotency'"
        ).fetchone())
        self.assertIsNotNone(connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name='maintenance_plan_immutable_content'"
        ).fetchone())
        self.assertEqual(
            [row["version"] for row in connection.execute(
                "SELECT version FROM maintenance_schema_migrations ORDER BY version"
            )],
            list(range(1, SCHEMA_VERSION + 1)),
        )

    def test_checksum_mismatch_fails_closed(self):
        connection = base_connection()
        self.addCleanup(connection.close)
        connection.execute("""
            CREATE TABLE maintenance_schema_migrations(
              version INTEGER PRIMARY KEY,name TEXT NOT NULL,checksum TEXT NOT NULL,applied_at TEXT NOT NULL
            )
        """)
        connection.execute(
            "INSERT INTO maintenance_schema_migrations VALUES(?,?,?,?)",
            (SCHEMA_VERSION, "unexpected", "bad", "2026-08-03T00:00:00Z"),
        )
        with self.assertRaises(MigrationDriftError):
            install_maintenance_schema(connection)
        self.assertIsNone(connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='maintenance_plans'"
        ).fetchone())


class MaintenanceLifecycleTests(unittest.TestCase):
    def test_plan_transition_matrix_is_complete_and_rejects_repeats(self):
        for current in MaintenanceState:
            for target in MaintenanceState:
                if target in PLAN_TRANSITIONS[current]:
                    validate_plan_transition(current, target)
                else:
                    with self.assertRaises(TransitionError):
                        validate_plan_transition(current, target)

    def test_workflow_transition_matrix_is_complete_and_rejects_repeats(self):
        for current in MaintenanceWorkflowState:
            for target in MaintenanceWorkflowState:
                if target in WORKFLOW_TRANSITIONS[current]:
                    validate_workflow_transition(current, target)
                else:
                    with self.assertRaises(TransitionError):
                        validate_workflow_transition(current, target)

    def test_hash_and_idempotency_are_canonical_and_cover_observations(self):
        first = PlanHashInput(
            operation_kind="reboot",
            plan={"steps": [{"kind": "verify"}], "reason": "patching"},
            observation={"boot_id": "before", "password": "must-not-persist"},
            target_node_id=1,
            expected_assignment_revision=4,
        )
        reordered = PlanHashInput(
            operation_kind="reboot",
            plan={"reason": "patching", "steps": [{"kind": "verify"}]},
            observation={"password": "different-secret", "boot_id": "before"},
            target_node_id=1,
            expected_assignment_revision=4,
        )
        self.assertEqual(canonical_plan_hash(first), canonical_plan_hash(reordered))
        changed = PlanHashInput(**{**first.__dict__, "observation": {"boot_id": "after"}})
        self.assertNotEqual(canonical_plan_hash(first), canonical_plan_hash(changed))
        self.assertEqual(
            derive_idempotency_key("reboot", LockScope.HOST, 1, "request-7"),
            derive_idempotency_key("reboot", "host", "1", "request-7"),
        )

    def test_recovery_classification_uses_observed_state_not_log_text(self):
        complete = classify_recovery(RecoveryEvidence(
            side_effect_state=SideEffectState.MAY_HAVE_STARTED,
            observation_complete=True,
            before_fingerprint="old",
            after_fingerprint="new",
            observed_fingerprint="new",
        ))
        self.assertEqual(complete.classification, RecoveryClassification.COMPLETE)
        ambiguous = classify_recovery(RecoveryEvidence(
            side_effect_state=SideEffectState.VERIFIED,
            observation_complete=False,
            observed_fingerprint="new",
            after_fingerprint="new",
        ))
        self.assertEqual(ambiguous.classification, RecoveryClassification.AMBIGUOUS)
        resumable = classify_recovery(RecoveryEvidence(
            side_effect_state=SideEffectState.PREPARED,
            observation_complete=True,
            before_fingerprint="old",
            after_fingerprint="new",
            observed_fingerprint="old",
        ))
        self.assertEqual(resumable.classification, RecoveryClassification.SAFE_TO_RESUME)


class MaintenanceRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.connection = base_connection()
        install_maintenance_schema(self.connection)
        self.repository = MaintenanceRepository(self.connection)

    def tearDown(self):
        self.connection.close()

    def create_plan(self, key: str = "plan-key", **overrides):
        values = {
            "operation_kind": "reboot",
            "plan": {"steps": [{"kind": "prepare"}], "credential": "sensitive"},
            "observation": {"boot_id": "boot-before", "api_key": "sensitive"},
            "idempotency_key": key,
            "requested_by": "operator",
            "expires_at": NOW + timedelta(minutes=15),
            "target_node_id": 1,
            "expected_assignment_revision": 4,
            "observed_at": "2026-08-03T00:00:00Z",
        }
        values.update(overrides)
        return self.repository.create_plan(**values)

    def test_policy_updates_use_optimistic_revision_and_redact(self):
        created = self.repository.put_policy(1, {"window": "night", "token": "private"}, "operator")
        self.assertEqual(created.revision, 1)
        self.assertEqual(created.policy["token"], "[REDACTED]")
        updated = self.repository.put_policy(1, {"window": "weekend"}, "operator", expected_revision=1)
        self.assertEqual(updated.revision, 2)
        with self.assertRaises(RevisionConflict):
            self.repository.put_policy(1, {"window": "stale"}, "operator", expected_revision=1)

    def test_plan_is_immutable_idempotent_and_optimistically_transitioned(self):
        plan = self.create_plan()
        same = self.create_plan()
        self.assertEqual(same.id, plan.id)
        self.assertEqual(plan.plan["credential"], "[REDACTED]")
        self.assertEqual(plan.observation["api_key"], "[REDACTED]")
        self.assertTrue(self.repository.verify_plan_hash(plan.id))
        with self.assertRaises(IdempotencyConflict):
            self.create_plan(plan={"steps": [{"kind": "different"}]})
        ready = self.repository.transition_plan(plan.id, 1, MaintenanceState.READY)
        self.assertEqual(ready.state_revision, 2)
        with self.assertRaises(RevisionConflict):
            self.repository.transition_plan(plan.id, 1, MaintenanceState.EXECUTING)
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("UPDATE maintenance_plans SET plan_json='{}' WHERE id=?", (plan.id,))

    def test_compiled_plan_hash_is_the_authoritative_persisted_hash(self):
        payload = {
            "schema_version": 1,
            "target": {"operation": "reboot", "node_id": 1},
            "steps": [{"sequence": 1, "kind": "verify"}],
        }
        authoritative_hash = compiled_plan_hash(payload)
        plan = self.create_plan(
            plan={**payload, "plan_hash": authoritative_hash},
            authoritative_plan_hash=authoritative_hash,
        )

        self.assertEqual(plan.plan_hash, authoritative_hash)
        self.assertTrue(self.repository.verify_plan_hash(plan.id, authoritative_hash))
        with self.assertRaises(ValueError):
            self.create_plan(
                key="different-authoritative-plan",
                plan={**payload, "plan_hash": "0" * 64},
                authoritative_plan_hash="0" * 64,
                target_node_id=2,
            )

    def test_active_target_uniqueness_blocks_overlapping_plans(self):
        first = self.create_plan("first", initial_state=MaintenanceState.READY)
        self.assertEqual(first.lifecycle_state, MaintenanceState.READY)
        with self.assertRaises(OverlappingPlanError):
            self.create_plan("second", initial_state=MaintenanceState.READY)

    def test_startup_recovery_preserves_linked_runs_and_leaves_legacy_runs_unchanged(self):
        ready = self.create_plan("ready", initial_state=MaintenanceState.READY, run_id=1)
        executing = self.create_plan(
            "executing", initial_state=MaintenanceState.READY, run_id=2, target_node_id=2,
        )
        executing = self.repository.transition_plan(executing.id, executing.state_revision, MaintenanceState.EXECUTING)
        self.connection.execute("INSERT INTO runs(id,status) VALUES(3,'queued')")

        startup = self.repository.prepare_startup_recovery()

        self.assertEqual(startup.protected_run_ids, frozenset({1, 2}))
        self.assertEqual(startup.transitioned_plan_ids, (executing.id,))
        self.assertEqual(self.repository.get_plan(ready.id).lifecycle_state, MaintenanceState.READY)
        self.assertEqual(self.repository.get_plan(executing.id).lifecycle_state, MaintenanceState.RECOVERY_REQUIRED)
        statuses = dict(self.connection.execute("SELECT id,status FROM runs ORDER BY id"))
        self.assertEqual(statuses, {1: "recovery_required", 2: "recovery_required", 3: "queued"})
        first_log = self.connection.execute("SELECT log FROM runs WHERE id=1").fetchone()[0]
        self.repository.prepare_startup_recovery()
        self.assertEqual(self.connection.execute("SELECT log FROM runs WHERE id=1").fetchone()[0], first_log)

    def test_restored_checkpoint_recovery_is_idempotent_before_any_remote_cleanup(self):
        plan = self.create_plan("backup-recovery", initial_state=MaintenanceState.READY, run_id=1)
        executing = self.repository.transition_plan(plan.id, plan.state_revision, MaintenanceState.EXECUTING)
        self.repository.record_checkpoint(
            plan_id=executing.id,
            checkpoint_key="signed-executor",
            sequence=0,
            side_effect_state=SideEffectState.PREPARED,
            payload={"manifest": "sha256:fixture", "token": "must-not-store"},
            observation={"boot_id": "before"},
        )
        self.connection.commit()

        with tempfile.TemporaryDirectory() as root:
            backup_path = Path(root) / "control.db"
            backup = sqlite3.connect(backup_path)
            try:
                self.connection.backup(backup)
            finally:
                backup.close()

            restored = sqlite3.connect(backup_path)
            restored.row_factory = sqlite3.Row
            restored.execute("PRAGMA foreign_keys = ON")
            self.addCleanup(restored.close)
            recovered = MaintenanceRepository(restored)

            first = recovered.prepare_startup_recovery()
            second = recovered.prepare_startup_recovery()

            self.assertEqual(first.protected_run_ids, frozenset({1}))
            self.assertEqual(second.transitioned_plan_ids, ())
            self.assertEqual(
                recovered.get_plan(executing.id).lifecycle_state,
                MaintenanceState.RECOVERY_REQUIRED,
            )
            checkpoint = recovered.latest_checkpoint(executing.id)
            self.assertIsNotNone(checkpoint)
            assert checkpoint is not None
            self.assertEqual(checkpoint.payload["token"], "[REDACTED]")
            self.assertEqual(
                restored.execute("SELECT status FROM runs WHERE id=1").fetchone()["status"],
                "recovery_required",
            )

    def test_startup_recovery_closes_only_checkpoint_proven_complete(self):
        plan = self.create_plan("startup-complete", initial_state=MaintenanceState.READY, run_id=1)
        plan = self.repository.transition_plan(plan.id, plan.state_revision, MaintenanceState.EXECUTING)
        checkpoint = self.repository.record_checkpoint(
            plan_id=plan.id,
            checkpoint_key="workload.restarted",
            sequence=1,
            side_effect_state=SideEffectState.MAY_HAVE_STARTED,
            payload={"workload": "redacted"},
        )
        self.connection.execute(
            "UPDATE maintenance_checkpoints SET recovery_evidence_json=? WHERE id=?",
            (json.dumps({
                source: {
                    "observation_complete": True,
                    "observed_fingerprint": "after",
                    "before_fingerprint": "before",
                    "after_fingerprint": "after",
                }
                for source in ("host", "workload", "observability", "elasticsearch")
            }), checkpoint.id),
        )

        startup = self.repository.prepare_startup_recovery()

        self.assertEqual(startup.protected_run_ids, frozenset())
        self.assertEqual(startup.classifications[0].classification, StartupRecoveryClassification.COMPLETE)
        self.assertEqual(self.repository.get_plan(plan.id).lifecycle_state, MaintenanceState.SUCCEEDED)
        self.assertEqual(self.connection.execute("SELECT status FROM runs WHERE id=1").fetchone()[0], "succeeded")
        classified = self.repository.latest_checkpoint(plan.id)
        assert classified is not None
        self.assertEqual(classified.recovery_classification, "complete")
        self.assertEqual(classified.recovery_reason_code, "after_state_observed")

    def test_startup_recovery_uses_all_named_projections_and_fails_closed(self):
        plan = self.create_plan("startup-incomplete", initial_state=MaintenanceState.READY, run_id=1)
        plan = self.repository.transition_plan(plan.id, plan.state_revision, MaintenanceState.EXECUTING)
        self.repository.record_checkpoint(
            plan_id=plan.id,
            checkpoint_key="workload.prepared",
            sequence=1,
            side_effect_state=SideEffectState.PREPARED,
            payload={},
        )

        class Projection:
            def __init__(self, source, *, complete=True, identity_matches=True):
                self.source = source
                self.complete = complete
                self.identity_matches = identity_matches

            def observe(self, _plan, _checkpoint):
                return RecoveryProjectionResult(
                    source=self.source,
                    complete=self.complete,
                    observed_fingerprint="before" if self.complete else None,
                    before_fingerprint="before",
                    after_fingerprint="after",
                    identity_matches=self.identity_matches,
                )

        coordinator = MaintenanceStartupRecoveryCoordinator(
            self.repository,
            RecoveryProjectionBundle(
                host=Projection("host"),
                workload=Projection("workload"),
                observability=Projection("observability", complete=False),
                elasticsearch=Projection("elasticsearch"),
            ),
        )

        startup = self.repository.prepare_startup_recovery(coordinator)

        self.assertEqual(startup.protected_run_ids, frozenset({1}))
        self.assertEqual(startup.classifications[0].classification, StartupRecoveryClassification.AMBIGUOUS)
        self.assertEqual(startup.classifications[0].sources, ("host", "workload", "observability", "elasticsearch"))
        self.assertEqual(self.repository.get_plan(plan.id).lifecycle_state, MaintenanceState.RECOVERY_REQUIRED)
        checkpoint = self.repository.latest_checkpoint(plan.id)
        assert checkpoint is not None
        self.assertEqual(checkpoint.recovery_reason_code, "projection_incomplete")

    def test_startup_recovery_marks_prepared_checkpoint_incomplete_and_resumable(self):
        plan = self.create_plan("startup-resumable", initial_state=MaintenanceState.READY, run_id=1)
        plan = self.repository.transition_plan(plan.id, plan.state_revision, MaintenanceState.EXECUTING)
        self.repository.record_checkpoint(
            plan_id=plan.id,
            checkpoint_key="workload.prepared",
            sequence=1,
            side_effect_state=SideEffectState.PREPARED,
            payload={},
        )

        class Projection:
            def __init__(self, source):
                self.source = source

            def observe(self, _plan, _checkpoint):
                return RecoveryProjectionResult(
                    source=self.source,
                    complete=True,
                    observed_fingerprint="before",
                    before_fingerprint="before",
                    after_fingerprint="after",
                )

        coordinator = MaintenanceStartupRecoveryCoordinator(
            self.repository,
            RecoveryProjectionBundle(*(Projection(source) for source in (
                "host", "workload", "observability", "elasticsearch",
            ))),
        )

        startup = self.repository.prepare_startup_recovery(coordinator)

        self.assertEqual(startup.classifications[0].classification, StartupRecoveryClassification.INCOMPLETE)
        self.assertTrue(startup.classifications[0].resumable)
        checkpoint = self.repository.latest_checkpoint(plan.id)
        assert checkpoint is not None
        self.assertEqual(checkpoint.recovery_classification, "incomplete")
        self.assertTrue(checkpoint.resumable)

    def test_startup_recovery_marks_non_idempotent_started_checkpoint_recovery_required(self):
        plan = self.create_plan("startup-unsafe", initial_state=MaintenanceState.READY, run_id=1)
        plan = self.repository.transition_plan(plan.id, plan.state_revision, MaintenanceState.EXECUTING)
        self.repository.record_checkpoint(
            plan_id=plan.id,
            checkpoint_key="workload.started",
            sequence=1,
            side_effect_state=SideEffectState.MAY_HAVE_STARTED,
            payload={},
        )

        class Projection:
            def __init__(self, source):
                self.source = source

            def observe(self, _plan, _checkpoint):
                return RecoveryProjectionResult(
                    source=self.source,
                    complete=True,
                    observed_fingerprint="before",
                    before_fingerprint="before",
                    after_fingerprint="after",
                )

        coordinator = MaintenanceStartupRecoveryCoordinator(
            self.repository,
            RecoveryProjectionBundle(*(Projection(source) for source in (
                "host", "workload", "observability", "elasticsearch",
            ))),
        )

        startup = self.repository.prepare_startup_recovery(coordinator)

        self.assertEqual(startup.classifications[0].classification, StartupRecoveryClassification.RECOVERY_REQUIRED)
        checkpoint = self.repository.latest_checkpoint(plan.id)
        assert checkpoint is not None
        self.assertEqual(checkpoint.recovery_classification, "recovery_required")
        self.assertFalse(checkpoint.resumable)

    def test_conflict_observation_is_scoped_read_only_deterministic_and_redacted(self):
        self.connection.execute(
            "UPDATE runs SET target='cluster-a:settings',context_json=? WHERE id=1",
            ('{"password":"must-not-appear"}',),
        )
        self.connection.execute("UPDATE runs SET target='unrelated' WHERE id=2")
        self.connection.executemany(
            "INSERT INTO runs(id,status,target,context_json) VALUES(?,?,?,?)",
            (
                (3, "recovery_required", "node-a:probe", "{}"),
                (4, "running", "unrelated", "{}"),
                (5, "queued", "unrelated", "{}"),
                (6, "succeeded", "cluster-a:finished", "{}"),
                (7, "running", "contains-sensitive-target", '{"cluster_id":2,"token":"must-not-appear"}'),
                (8, "recovery_required", "cluster-b:recovery", "{}"),
            ),
        )
        self.connection.execute("UPDATE cluster_assignments SET operation_run_id=4 WHERE id=1")
        self.connection.execute(
            "INSERT INTO workload_change_batches(run_id,cluster_id,plan_encrypted) VALUES(5,1,'redacted')"
        )
        plan = self.create_plan(
            "conflict-plan",
            target_node_id=None,
            target_cluster_id=1,
            run_id=1,
            initial_state=MaintenanceState.READY,
        )
        unrelated = self.create_plan(
            "unrelated-plan",
            target_node_id=2,
            run_id=2,
            initial_state=MaintenanceState.READY,
        )
        lock = self.repository.acquire_locks(
            [LockRequest("cluster", 1)], owner_plan_id=plan.id, now=NOW,
        )[0]
        changes_before = self.connection.total_changes

        observed = self.repository.observe_conflicts(1)
        repeated = self.repository.observe_conflicts(1)

        self.assertEqual(self.connection.total_changes, changes_before)
        self.assertEqual(observed, repeated)
        self.assertEqual(observed.cluster_ids, (1, 2))
        self.assertEqual(observed.host_assignment_ids, (1, 2))
        self.assertTrue(observed.has_conflicts)
        self.assertEqual(set(observed.conflict_identifiers), {
            f"maintenance-plan:{plan.id}",
            f"maintenance-lock:cluster:1:{lock.id}",
            "run:1",
            "run:3",
            "run:4",
            "run:5",
            "run:7",
            "run:8",
            "assignment-operation:1:4",
            "workload-batch:5",
        })
        self.assertNotIn(unrelated.id, " ".join(observed.conflict_identifiers))
        self.assertNotIn("must-not-appear", " ".join(observed.conflict_identifiers))
        self.assertNotIn("contains-sensitive-target", " ".join(observed.conflict_identifiers))

        excluding_self = self.repository.observe_conflicts(1, exclude_plan_id=plan.id)
        self.assertNotIn(f"maintenance-plan:{plan.id}", excluding_self.conflict_identifiers)
        self.assertNotIn(f"maintenance-lock:cluster:1:{lock.id}", excluding_self.conflict_identifiers)
        self.assertNotIn("run:1", excluding_self.conflict_identifiers)

    def test_steps_and_checkpoints_are_idempotent_and_recovery_is_revisioned(self):
        plan = self.create_plan()
        step = self.repository.create_step(
            plan_id=plan.id,
            step_key="prepare-host",
            sequence=0,
            step_kind="prepare",
            affected_node_id=1,
            before_observation={"password": "private", "boot_id": "old"},
        )
        self.assertEqual(step.before_observation["password"], "[REDACTED]")
        self.assertEqual(self.repository.create_step(
            plan_id=plan.id,
            step_key="prepare-host",
            sequence=0,
            step_kind="prepare",
            affected_node_id=1,
            before_observation={"password": "other", "boot_id": "old"},
        ).id, step.id)
        running = self.repository.transition_step(step.id, 1, MaintenanceStepState.EXECUTING)
        self.assertEqual(running.attempt_count, 1)
        checkpoint = self.repository.record_checkpoint(
            plan_id=plan.id,
            step_id=step.id,
            checkpoint_key="before-reboot",
            sequence=0,
            side_effect_state=SideEffectState.PREPARED,
            payload={"manifest": "sha256:abc", "token": "private"},
            observation={"boot_id": "old"},
        )
        self.assertEqual(checkpoint.payload["token"], "[REDACTED]")
        classified, decision = self.repository.classify_checkpoint(
            checkpoint.id,
            RecoveryEvidence(
                side_effect_state=SideEffectState.PREPARED,
                observation_complete=True,
                before_fingerprint="old",
                after_fingerprint="new",
                observed_fingerprint="old",
            ),
            expected_revision=1,
            now=NOW,
        )
        self.assertEqual(decision.classification, RecoveryClassification.SAFE_TO_RESUME)
        self.assertEqual(classified.classification_revision, 2)
        with self.assertRaises(RevisionConflict):
            self.repository.classify_checkpoint(
                checkpoint.id,
                RecoveryEvidence(SideEffectState.PREPARED, True, "old", "old", "new"),
                expected_revision=1,
            )

    def test_host_state_requires_a_plan_and_uses_revisions(self):
        plan = self.create_plan()
        host = self.repository.get_host_state(1)
        planning = self.repository.transition_host_state(1, host.state_revision, "planning", plan.id)
        self.assertEqual(planning.active_plan_id, plan.id)
        with self.assertRaises(RevisionConflict):
            self.repository.transition_host_state(1, host.state_revision, "maintenance", plan.id)
        available = self.repository.transition_host_state(1, planning.state_revision, "available", plan.id)
        self.assertIsNone(available.active_plan_id)
        self.assertEqual(available.workflow_state, MaintenanceWorkflowState.AVAILABLE)

    def test_workflow_state_has_independent_revision_and_legacy_state(self):
        host = self.repository.get_host_state(1)
        prepared = self.repository.transition_host_workflow_state(
            1,
            host.workflow_state_revision,
            MaintenanceWorkflowState.PREPARING,
        )
        self.assertEqual(prepared.state.value, "available")
        self.assertEqual(prepared.workflow_state, MaintenanceWorkflowState.PREPARING)
        self.assertEqual(prepared.workflow_state_revision, host.workflow_state_revision + 1)

    def test_assignment_workflow_requires_an_owned_plan_until_returned_to_available(self):
        plan = self.create_plan("assignment-workflow")
        state = self.repository.get_assignment_state(1)
        preparing = self.repository.transition_assignment_state(
            1,
            state.state_revision,
            MaintenanceWorkflowState.PREPARING,
            plan.id,
        )
        self.assertEqual(preparing.active_plan_id, plan.id)
        self.assertEqual(preparing.workflow_state, MaintenanceWorkflowState.PREPARING)
        ready = self.repository.transition_assignment_state(
            1,
            preparing.state_revision,
            MaintenanceWorkflowState.READY_TO_STOP,
            plan.id,
        )
        available = self.repository.transition_assignment_state(
            1,
            ready.state_revision,
            MaintenanceWorkflowState.AVAILABLE,
            plan.id,
        )
        self.assertIsNone(available.active_plan_id)

    def test_assignment_workflow_upgrade_allows_one_host_plan_to_own_multiple_assignments(self):
        self.connection.execute("DROP INDEX IF EXISTS ix_assignment_maintenance_active_plan")
        self.connection.execute(
            "CREATE UNIQUE INDEX ux_assignment_maintenance_active_plan "
            "ON assignment_maintenance_state(active_plan_id) WHERE active_plan_id IS NOT NULL"
        )
        self.connection.execute(
            "DELETE FROM maintenance_schema_migrations WHERE version=?", (SCHEMA_VERSION,),
        )

        self.assertEqual(install_maintenance_schema(self.connection), SCHEMA_VERSION)
        plan = self.create_plan("shared-host-plan")
        first = self.repository.get_assignment_state(1)
        second = self.repository.get_assignment_state(2)
        self.repository.transition_assignment_state(
            1, first.state_revision, MaintenanceWorkflowState.PREPARING, plan.id,
        )
        self.repository.transition_assignment_state(
            2, second.state_revision, MaintenanceWorkflowState.PREPARING, plan.id,
        )

        indexes = {
            row["name"]: row["unique"]
            for row in self.connection.execute("PRAGMA index_list('assignment_maintenance_state')")
        }
        self.assertEqual(indexes["ix_assignment_maintenance_active_plan"], 0)
        self.assertNotIn("ux_assignment_maintenance_active_plan", indexes)

    def test_multi_scope_locks_are_atomic_and_expiry_requires_recovery(self):
        first = self.create_plan("first")
        second = self.create_plan("second", target_node_id=2)
        locks = self.repository.acquire_locks(
            [LockRequest("host", 1), LockRequest("cluster", 1)],
            owner_plan_id=first.id,
            ttl_seconds=30,
            now=NOW,
        )
        self.assertEqual(len(locks), 2)
        token = locks[0].owner_token
        with self.assertRaises(LockConflict):
            self.repository.acquire_locks(
                [LockRequest("assignment", 2), LockRequest("cluster", 1)],
                owner_plan_id=second.id,
                ttl_seconds=30,
                now=NOW,
            )
        active_scopes = {(lock.scope.value, lock.identifier) for lock in self.repository.list_active_locks()}
        self.assertNotIn(("assignment", "2"), active_scopes)
        with self.assertRaises(StaleLockRequiresRecovery):
            self.repository.heartbeat_locks(token, now=NOW + timedelta(seconds=31))
        with self.assertRaises(StaleLockRequiresRecovery):
            self.repository.release_locks(token, now=NOW + timedelta(seconds=31))
        recovered = self.repository.recover_stale_lock(
            locks[0].id,
            observation={"boot_id": "unchanged"},
            recovered_by="operator",
            reason="rediscovery_confirmed_no_side_effect",
            now=NOW + timedelta(seconds=31),
        )
        self.assertIsNotNone(recovered.stale_released_at)

    def test_audit_detail_is_structurally_redacted(self):
        event_id = self.repository.record_audit(
            username="operator",
            action="maintenance-plan-created",
            cluster_id=1,
            item_id="plan-1",
            detail={"result": "ready", "private_key": "do-not-store"},
        )
        detail = self.connection.execute("SELECT detail FROM audit_events WHERE id=?", (event_id,)).fetchone()[0]
        self.assertNotIn("do-not-store", detail)
        self.assertIn("[REDACTED]", detail)


if __name__ == "__main__":
    unittest.main()
