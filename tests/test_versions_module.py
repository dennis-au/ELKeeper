from pathlib import Path
import tempfile
import unittest
from contextlib import contextmanager

from app.modules.platform.db import connect
from fastapi import HTTPException

from app.modules.versions import ElasticRegistry, VersionOperations, VersionRepository, VersionUpgradeService, VersionUpgradeWorker, image_for_role, image_version, observation_is_fresh, version_key


class VersionsModuleTests(unittest.TestCase):
    def test_public_operations_facade_owns_runtime_composition(self):
        async def execute(_run_id, _command):
            return True

        class Workloads:
            @classmethod
            def from_connection(cls, _connection):
                return cls()

        with tempfile.TemporaryDirectory() as directory:
            operations = VersionOperations(
                ansible=lambda inventory, node, module, arguments: [inventory, node, module, arguments],
                workload_name=lambda _cluster, _assignment: "ecp-kibana",
                image_for_role=image_for_role,
                image_version=image_version,
                default_stack_version="8.19.0",
                repository_factory=lambda: object(),
                cluster_record=lambda _connection, _cluster_id: None,
                available_versions=lambda _assignments, _filebeat: [],
                version_key=version_key,
                membership_ready=lambda _member: True,
                observation_is_fresh=lambda _observation: True,
                topology_elasticsearch_roles={"master"},
                db_factory=lambda: self,
                variables_dir=Path(directory),
                assignment_record=lambda _connection, _assignment_id: {},
                cluster_payload=lambda _connection, _assignment: {},
                reconcile_command=lambda *_: [],
                upgrade_preflight_command=lambda *_: [],
                execute_logged_command=execute,
                add_log=lambda *_: None,
                platform_finish_run=lambda *_: None,
                workload_repository=Workloads,
                launch_filebeat_reconcile=lambda *_: 0,
                active_operation=lambda *_: False,
                upgrade_order=("master",),
                start_run=lambda *_: None,
                run_descriptor=lambda *_: None,
                inventory=lambda _run_id: Path(directory) / "inventory",
                schedule=lambda _coroutine: None,
            )

            command = operations.probe_command(
                "inventory", {}, {"id": 4, "role": "kibana", "image_version": "", "node_name": "node-a"}
            )

        self.assertEqual(command[:3], ["inventory", "node-a", "shell"])
        self.assertIn("ecp-kibana", command[3])

    def test_version_and_image_contracts(self):
        self.assertEqual(version_key("8.19.0"), (8, 19, 0))
        self.assertIsNone(version_key("8.19"))
        self.assertEqual(image_version("docker.elastic.co/kibana/kibana:8.19.0"), "8.19.0")
        self.assertEqual(image_for_role("kibana", "8.19.0"), "docker.elastic.co/kibana/kibana:8.19.0")

    def test_stale_or_malformed_observations_fail_closed(self):
        self.assertFalse(observation_is_fresh(None))
        self.assertFalse(observation_is_fresh({"observed_at": "not-a-date"}))

    def test_registry_tag_filtering_is_owned_by_versions_and_uses_injected_transport(self):
        registry = ElasticRegistry(
            cache={}, cache_seconds=60, request_timeout=1, listing_timeout=1,
            tag_page_size=100, tag_page_limit=1, tag_result_limit=10,
        )
        result = registry.tags(
            "elasticsearch/elasticsearch", "8.18.999",
            fetch_json=lambda _: ({"tags": ["8.19.0", "8.20.0-SNAPSHOT", "latest"]}, {}),
        )
        self.assertEqual(result, {"8.19.0"})

    def test_runtime_observation_preserves_primary_fields_when_filebeat_updates(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "versions.db"
            with connect(database) as connection:
                connection.execute(
                    "CREATE TABLE workload_observations(assignment_id INTEGER PRIMARY KEY,image TEXT,digest TEXT,version TEXT,running INTEGER,cached INTEGER,observed_at TEXT,error TEXT,filebeat_state TEXT,filebeat_observed_at TEXT,filebeat_error TEXT)"
                )
            repository = VersionRepository(lambda: connect(database))
            repository.record_runtime(
                1,
                image="docker.elastic.co/elasticsearch/elasticsearch:8.19.0",
                digest="sha256:test",
                version="8.19.0",
                running=True,
                cached=True,
                error="",
            )
            repository.record_filebeat_runtime(1, state="running", error="")
            with connect(database) as connection:
                row = connection.execute("SELECT * FROM workload_observations WHERE assignment_id=1").fetchone()
            self.assertEqual(row["version"], "8.19.0")
            self.assertEqual(row["filebeat_state"], "running")

    def test_upgrade_policy_keeps_response_shape_and_requires_master_redundancy(self):
        cluster = {
            "assignments": [
                {
                    "id": 7,
                    "node_id": 2,
                    "node_name": "host-a",
                    "role": "master",
                    "image_version": "",
                    "observation": {"version": "8.18.0", "running": True, "error": ""},
                }
            ],
            "members": [{"node_id": 2, "ready": True}],
            "log_monitoring": {"filebeat_enabled": False},
        }
        service = VersionUpgradeService(
            cluster_record=lambda _connection, _cluster_id: cluster,
            available_versions=lambda _assignments, _filebeat: ["8.19.0"],
            default_stack_version="8.19.0",
            version_key=version_key,
            membership_ready=lambda member: bool(member and member["ready"]),
            observation_is_fresh=lambda observation: bool(observation),
            topology_elasticsearch_roles={"master", "hot", "warm", "ml", "ingest", "coordinating"},
        )
        details = service.details(object(), 4)
        self.assertEqual(details["assignments"][0]["desired_version"], "8.19.0")
        with self.assertRaises(HTTPException) as failure:
            service.preflight(cluster, "8.19.0")
        self.assertEqual(failure.exception.detail, "Safe Elasticsearch rolling upgrade requires three healthy master-eligible workloads")

    def test_upgrade_policy_rejects_unavailable_and_stale_targets(self):
        cluster = {"assignments": [], "members": [], "log_monitoring": {"filebeat_enabled": False}}
        service = VersionUpgradeService(
            cluster_record=lambda _connection, _cluster_id: cluster,
            available_versions=lambda _assignments, _filebeat: ["8.19.0"],
            default_stack_version="8.19.0",
            version_key=version_key,
            membership_ready=lambda _member: True,
            observation_is_fresh=lambda _observation: True,
            topology_elasticsearch_roles={"master"},
        )
        with self.assertRaises(HTTPException) as failure:
            service.validate_target(cluster, "8.20.0")
        self.assertEqual(failure.exception.detail, "Choose a version available for every active component in this cluster")

    def test_upgrade_worker_rolls_back_failed_workload_and_cleans_artifacts(self):
        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        class Workloads:
            def __init__(self):
                self.updated = []

            def set_image_version_in_connection(self, _connection, assignment_id, version):
                self.updated.append((assignment_id, version))

        assignment = {
            "id": 4,
            "node_name": "host-a",
            "role": "kibana",
            "observation": {"version": "8.18.0"},
        }
        cluster = {"assignments": [assignment]}
        workloads = Workloads()
        commands = []
        logs = []
        statuses = []

        async def execute(_run_id, command):
            commands.append(command)
            return len(commands) == 1 or len(commands) == 3

        with tempfile.TemporaryDirectory() as directory:
            worker = VersionUpgradeWorker(
                db_factory=lambda: Connection(),
                variables_dir=Path(directory),
                assignment_record=lambda _connection, _assignment_id: assignment,
                cluster_record=lambda _connection, _cluster_id: cluster,
                cluster_payload=lambda _connection, _row: {"assignment": {"image_version": "8.18.0"}},
                reconcile_command=lambda _inventory, path, _node: ["reconcile", str(path)],
                upgrade_preflight_command=lambda _inventory, path, _node: ["preflight", str(path)],
                execute_logged_command=execute,
                add_log=lambda _run_id, value: logs.append(value),
                platform_finish_run=lambda _connection, _run_id, status: statuses.append(status),
                workload_repository=lambda _connection: workloads,
                version_key=version_key,
                launch_filebeat_reconcile=lambda _cluster_id, _username: 99,
            )
            inventory = worker._variables / "inventory"
            inventory.write_text("inventory", encoding="utf-8")
            import asyncio

            asyncio.run(worker.run(12, 1, "8.19.0", inventory, [4]))
            self.assertEqual(len(commands), 3)
            self.assertEqual(statuses, ["failed"])
            self.assertEqual(workloads.updated, [])
            self.assertTrue(any("restoring 8.18.0" in value for value in logs))
            self.assertFalse(inventory.exists())
            self.assertEqual(list(worker._variables.iterdir()), [])
