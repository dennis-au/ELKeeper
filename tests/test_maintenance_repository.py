from pathlib import Path
import tempfile
import unittest

from app.modules.maintenance import MaintenanceRepository
from app.modules.platform.db import connect


class MaintenanceRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database = Path(self.directory.name) / "maintenance.db"
        with connect(self.database) as connection:
            connection.executescript(
                """
                CREATE TABLE nodes(id INTEGER PRIMARY KEY, name TEXT, enabled INTEGER);
                CREATE TABLE clusters(id INTEGER PRIMARY KEY, name TEXT, provider_type TEXT);
                CREATE TABLE cluster_assignments(
                    id INTEGER PRIMARY KEY, cluster_id INTEGER, node_id INTEGER,
                    role TEXT, revision INTEGER, state TEXT
                );
                CREATE TABLE runs(id INTEGER PRIMARY KEY, status TEXT, target TEXT, finished_at TEXT, log TEXT DEFAULT '');
                INSERT INTO nodes VALUES (1, 'node-a', 1);
                INSERT INTO clusters VALUES (7, 'cluster-a', 'native_podman');
                INSERT INTO cluster_assignments VALUES (11, 7, 1, 'master', 3, 'active');
                INSERT INTO cluster_assignments VALUES (12, 7, 1, 'hot', 4, 'purged');
                INSERT INTO runs(id,status,target) VALUES (99, 'running', 'cluster-a');
                """
            )

    def tearDown(self):
        self.directory.cleanup()

    def test_read_contracts_return_typed_records_and_filter_active_workloads(self):
        repository = MaintenanceRepository(lambda: connect(self.database))
        host = repository.host(1)
        workloads = repository.active_workloads_for_node(1)
        clusters = repository.clusters((7,))
        run = repository.run(99)

        self.assertEqual((host.id, host.name, host.enabled), (1, "node-a", True))
        self.assertTrue(repository.cluster_exists(7))
        self.assertFalse(repository.cluster_exists(404))
        self.assertEqual([(item.id, item.role, item.revision) for item in workloads], [(11, "master", 3)])
        self.assertEqual((clusters[0].id, clusters[0].name), (7, "cluster-a"))
        self.assertEqual((run.id, run.status), (99, "running"))
        self.assertIsNone(repository.host(404))

    def test_connection_backed_repository_reuses_active_transaction_connection(self):
        with connect(self.database) as connection:
            repository = MaintenanceRepository.from_connection(connection)
            self.assertEqual(repository.active_workloads_for_node(1)[0].cluster_id, 7)

    def test_run_status_write_boundary_preserves_log_and_validates_status(self):
        repository = MaintenanceRepository(lambda: connect(self.database))
        repository.mark_run_running(99)
        repository.mark_run_status(99, "recovery_required", finished_at="2026-08-03T04:00:00Z", log_suffix="recovered\n")
        run = repository.run(99)
        self.assertEqual(run.status, "recovery_required")
        self.assertEqual(run.record["finished_at"], "2026-08-03T04:00:00Z")
        self.assertEqual(run.record["log"], "recovered\n")
        with self.assertRaises(ValueError):
            repository.mark_run_status(99, "not-a-state")
