from pathlib import Path
import tempfile
import unittest
import json
from types import SimpleNamespace

from fastapi import HTTPException

from app.modules.platform.db import connect
from app.modules.workloads import WorkloadChange, WorkloadChangeSet, WorkloadChangeValidator, WorkloadRepository


class WorkloadRepositoryTests(unittest.TestCase):
    def test_active_assignment_queries_are_cluster_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "workloads.db"
            with connect(database) as connection:
                connection.execute("CREATE TABLE cluster_assignments(id INTEGER PRIMARY KEY, cluster_id INTEGER, node_id INTEGER, state TEXT)")
                connection.executemany(
                    "INSERT INTO cluster_assignments VALUES (?,?,?,?)",
                    [(1, 7, 1, "active"), (2, 7, 1, "purged"), (3, 8, 2, "active")],
                )
            repository = WorkloadRepository(lambda: connect(database))
            self.assertEqual(repository.active_count(7), 1)
            self.assertEqual(repository.active_ids(7), [1])
            self.assertTrue(repository.has_assignments_for_node(1))
            self.assertFalse(repository.has_assignments_for_node(404))

    def test_batch_lifecycle_writes_stay_inside_workload_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "batches.db"
            with connect(database) as connection:
                connection.execute(
                    "CREATE TABLE workload_change_batches (run_id INTEGER PRIMARY KEY, cluster_id INTEGER, plan_encrypted TEXT, completed_json TEXT NOT NULL DEFAULT '[]', phase TEXT NOT NULL DEFAULT 'applying')"
                )
                repository = WorkloadRepository.from_connection(connection)
                repository.create_batch_in_connection(
                    connection,
                    run_id=11,
                    cluster_id=7,
                    plan_encrypted="encrypted-plan",
                )
                self.assertEqual(repository.batch(11)["cluster_id"], 7)
                repository.record_batch_progress(11, ["change-a"])
                repository.set_batch_phase(11, "rolling_back")
                row = repository.batch(11)
                self.assertEqual(json.loads(row["completed_json"]), ["change-a"])
                self.assertEqual(row["phase"], "rolling_back")
                repository.delete_batch(11)
                self.assertIsNone(repository.batch(11))

    def test_batch_phase_rejects_unknown_state(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "batches.db"
            with connect(database) as connection:
                connection.execute(
                    "CREATE TABLE workload_change_batches (run_id INTEGER PRIMARY KEY, phase TEXT NOT NULL DEFAULT 'applying')"
                )
            repository = WorkloadRepository(lambda: connect(database))
            with self.assertRaises(ValueError):
                repository.set_batch_phase(1, "finished")


class WorkloadChangeValidatorTests(unittest.TestCase):
    def _validator(self, *, assignments=None):
        members = {1: {"node_id": 1, "node_name": "node-a", "enabled": True, "network_mode": "shared"}}
        return WorkloadChangeValidator(
            cluster_record=lambda _connection, _cluster_id: {"name": "lab", "desired_version": "8.19.0", "role_ports": {}},
            active_operation=lambda *_args: False,
            active_assignments=lambda *_args: assignments or [],
            validate_config=lambda _role, _config: None,
            recommended_version=lambda *_args: "8.20.0",
            default_version="8.18.0",
            projection_factory=lambda _connection: SimpleNamespace(member_record=lambda _cluster_id, node_id: members.get(node_id)),
            require_ready_membership=lambda _member: None,
            require_cluster_host_zone=lambda _cluster, _member: None,
            elasticsearch_roles=frozenset({"master", "hot"}),
            conflict_message=lambda *_args: None,
            open_config=lambda _value: {"cpu": "1", "memory": "2g", "storage_path": "/srv/elastic"},
            validate_final_ports=lambda _cluster, _assignments: None,
        )

    def test_validator_builds_master_create_with_recommended_version(self):
        change_set = WorkloadChangeSet(
            changes=[
                WorkloadChange(
                    client_id="master-1",
                    kind="create",
                    node_id=1,
                    role="master",
                    config={"cpu": "1", "memory": "2g", "storage_path": "/srv/elastic"},
                )
            ]
        )

        cluster, planned = self._validator().validate(object(), 1, change_set)

        self.assertEqual(cluster["name"], "lab")
        self.assertEqual(planned[0]["node_name"], "node-a")
        self.assertEqual(planned[0]["image_version"], "8.20.0")

    def test_validator_rejects_non_master_create_without_a_master(self):
        change_set = WorkloadChangeSet(
            changes=[
                WorkloadChange(
                    client_id="hot-1",
                    kind="create",
                    node_id=1,
                    role="hot",
                    config={"cpu": "1", "memory": "2g", "storage_path": "/srv/elastic"},
                )
            ]
        )

        with self.assertRaisesRegex(HTTPException, "Deploy a master"):
            self._validator().validate(object(), 1, change_set)
