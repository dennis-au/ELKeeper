import asyncio
import unittest

from pathlib import Path
import tempfile
from types import SimpleNamespace

from app.modules.observability import ObservabilityRepository, TelemetryDependencies, TelemetryManager, bounded_history
from app.modules.platform.db import connect


class ObservabilityModuleTests(unittest.TestCase):
    def test_history_is_bounded(self):
        self.assertEqual(bounded_history(list(range(5)), 3), [2, 3, 4])
        with self.assertRaises(ValueError):
            bounded_history([], 0)

    def test_runtime_repository_persists_only_host_projection_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "telemetry.db"
            with connect(database) as connection:
                connection.execute(
                    "CREATE TABLE host_runtime_observations(node_id INTEGER PRIMARY KEY,initialized INTEGER,reachable INTEGER,podman_socket_active INTEGER,os_name TEXT,podman_version TEXT,observed_at TEXT,last_error TEXT,network_interfaces_json TEXT)"
                )
            repository = ObservabilityRepository(lambda: connect(database))
            repository.record_host_runtime(
                7,
                {
                    "initialized": True,
                    "reachable": True,
                    "podman_socket_active": False,
                    "os_name": "Enterprise Linux",
                    "podman_version": "5.8",
                    "last_error": "Podman not ready",
                    "network_interfaces": {"ens18": ["192.0.2.10"]},
                    "containers": [{"name": "ecp-hidden"}],
                },
                observed_at="2026-08-03T00:00:00Z",
            )
            observed = repository.runtime_observation(7)
            self.assertTrue(observed["initialized"])
            self.assertNotIn("containers", observed)
            self.assertEqual(repository.runtime_observations()[7]["podman_version"], "5.8")

    def test_telemetry_collector_owns_publish_and_subscription_contract(self):
        dependencies = TelemetryDependencies(
            db_factory=lambda: SimpleNamespace(), workload_name=lambda cluster, assignment: "workload",
            image_version=lambda image: image, open_config=lambda value: {}, seal_config=lambda value: value,
            cluster_record=lambda connection, cluster_id: None,
            runtime=Path(tempfile.mkdtemp()), ca_cache=Path(tempfile.mkdtemp()),
            fast_collect_interval=1, slow_collect_interval=1, cluster_collect_interval=1,
            max_fast_host_probes=1, max_slow_host_probes=1, poll_jitter_seconds=0,
            host_resource_history_seconds=60, cluster_repository=object(), host_repository=object(),
            observability_repository=object(), version_repository=object(), workload_repository=object(),
            podman_tunnel_cls=object, ssh_pool=SimpleNamespace(close=lambda: None),
            remote_command=lambda *args, **kwargs: None,
            host_identity=lambda *args, **kwargs: None,
            host_network_interfaces=lambda *args, **kwargs: None,
            host_resource_counters=lambda *args, **kwargs: None,
            ssh_error_summary=str, container_name=lambda item: "", container_stats=lambda item: {},
            host_resource_rates=lambda previous, current: current, node_breakdown=lambda nodes, allocation: [],
            zone_breakdown=lambda breakdown: [], ca_ssl_context=lambda path: None,
            cluster_ca_path=lambda cache, cluster_id: cache, invalidate_cluster_ca=lambda cluster_id: None,
            utc_now=lambda: "2026-08-03T00:00:00Z", cluster_awaits_data_role=lambda cluster: False,
        )
        manager = TelemetryManager(dependencies)

        async def exercise():
            queue = manager.subscribe()
            await manager.publish("test", {"ok": True})
            message = await queue.get()
            manager.unsubscribe(queue)
            return message

        message = asyncio.run(exercise())
        self.assertEqual(message["event"], "test")
        self.assertEqual(message["data"], {"ok": True})
