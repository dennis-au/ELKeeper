from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone

from fastapi import HTTPException

from app.modules.maintenance.store import MaintenanceRepository, install_maintenance_schema
from app.modules.maintenance.upgrade_planning import (
    MaintenanceUpgradePlanningService,
    manifest_from_target_manifest,
)


NOW = datetime(2026, 8, 4, 8, 0, tzinfo=timezone.utc)


def connection() -> sqlite3.Connection:
    value = sqlite3.connect(":memory:")
    value.row_factory = sqlite3.Row
    value.execute("PRAGMA foreign_keys = ON")
    value.executescript("""
        CREATE TABLE nodes (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL);
        CREATE TABLE clusters (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL);
        CREATE TABLE runs (
          id INTEGER PRIMARY KEY,
          kind TEXT NOT NULL,
          target TEXT NOT NULL,
          status TEXT NOT NULL,
          command_json TEXT NOT NULL,
          context_json TEXT NOT NULL DEFAULT '{}',
          log TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
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
        INSERT INTO nodes(id,name) VALUES(1,'node-a'),(2,'node-b');
        INSERT INTO clusters(id,name) VALUES(1,'cluster-a');
        INSERT INTO memberships(cluster_id,node_id) VALUES(1,1),(1,2);
        INSERT INTO cluster_assignments(id,cluster_id,node_id,revision) VALUES(11,1,1,1),(12,1,2,1);
    """)
    install_maintenance_schema(value)
    return value


class MaintenanceUpgradeDispatchTests(unittest.TestCase):
    def setUp(self):
        self.connection = connection()
        self.repository = MaintenanceRepository(self.connection)
        self.cluster = {
            "id": 1,
            "name": "cluster-a",
            "assignments": [
                {
                    "id": 12, "node_id": 2, "node_name": "node-b", "role": "kibana",
                    "observation": {
                        "image": "docker.elastic.co/kibana/kibana:8.19.0",
                        "digest": "sha256:" + "b" * 64,
                        "version": "8.19.0", "running": True,
                    },
                },
                {
                    "id": 11, "node_id": 1, "node_name": "node-a", "role": "master",
                    "observation": {
                        "image": "docker.elastic.co/elasticsearch/elasticsearch:8.19.0",
                        "digest": "sha256:" + "a" * 64,
                        "version": "8.19.0", "running": True,
                    },
                },
            ],
        }

    def tearDown(self):
        self.connection.close()

    def service(self, *, preflight=lambda cluster, target, candidates: False, resolver=None):
        return MaintenanceUpgradePlanningService(
            self.repository,
            image_for_role=lambda role, version: f"docker.elastic.co/{role}:{version}",
            resolve_target_digests=resolver or (lambda assignments, target: {
                int(item["id"]): "sha256:" + ("c" if item["role"] == "master" else "d") * 64
                for item in assignments
            }),
            preflight=preflight,
            upgrade_order=("master", "kibana"),
            execution_enabled=False,
            clock=lambda: NOW,
        )

    def test_disabled_dispatch_persists_manifest_ordered_checkpoints_and_no_remote_run(self):
        dispatch = self.service().create_legacy_upgrade_plan(
            self.cluster, target_version="8.20.0", candidates=["8.20.0"], requested_by="operator",
        )

        self.assertFalse(dispatch.execution_enabled)
        self.assertEqual(dispatch.blockers, ("maintenance_upgrade_execution_disabled",))
        plan = self.repository.get_plan(dispatch.plan_id)
        self.assertEqual(plan.lifecycle_state.value, "blocked")
        manifest = manifest_from_target_manifest(plan.target_manifest)
        self.assertEqual([item.assignment_id for item in manifest.artifacts], [11, 12])
        self.assertEqual([step.affected_assignment_id for step in self.repository.list_steps(plan.id)], [11, 12])
        checkpoints = self.repository.list_checkpoints(plan.id)
        self.assertEqual(len(checkpoints), 2)
        self.assertEqual(checkpoints[0].payload["rollback"], "recovery_required_no_auto_downgrade")
        self.assertEqual(checkpoints[1].payload["rollback"], "restore_prior_artifact_when_compatible")
        run = self.connection.execute("SELECT status,command_json,log FROM runs WHERE id=?", (dispatch.run_id,)).fetchone()
        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(run["command_json"], "[]")
        self.assertIn("execution is disabled", run["log"])

    def test_rejected_preflight_creates_no_plan_run_or_registry_lookup(self):
        calls = {"resolver": 0}

        def rejected(cluster, target, candidates):
            raise HTTPException(422, "cluster health required")

        def resolver(assignments, target):
            calls["resolver"] += 1
            return {}

        with self.assertRaises(HTTPException):
            self.service(preflight=rejected, resolver=resolver).create_legacy_upgrade_plan(
                self.cluster, target_version="8.20.0", candidates=["8.20.0"], requested_by="operator",
            )
        self.assertEqual(calls["resolver"], 0)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM maintenance_plans").fetchone()[0], 0)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 0)

    def test_repeat_dispatch_reuses_the_immutable_planning_run(self):
        first = self.service().create_legacy_upgrade_plan(
            self.cluster, target_version="8.20.0", candidates=["8.20.0"], requested_by="operator",
        )
        second = self.service().create_legacy_upgrade_plan(
            self.cluster, target_version="8.20.0", candidates=["8.20.0"], requested_by="operator",
        )
        self.assertEqual((second.plan_id, second.run_id), (first.plan_id, first.run_id))
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM maintenance_plans").fetchone()[0], 1)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 1)
