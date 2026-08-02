import asyncio
import importlib
import json
import os
import ssl
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient


class ConsoleApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        os.environ.update(
            APP_DATA_DIR=cls.temp.name,
            APP_RUNTIME_DIR=os.path.join(cls.temp.name, "runtime"),
            APP_SECRET_KEY="test-secret",
            ADMIN_USERNAME="operator",
            ADMIN_PASSWORD="test-password",
        )
        import app.main

        cls.main = importlib.reload(app.main)
        cls.context = TestClient(cls.main.app)
        cls.client = cls.context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.context.__exit__(None, None, None)
        cls.temp.cleanup()

    def setUp(self):
        with self.main.db() as con:
            con.execute("DELETE FROM audit_events")
            con.execute("DELETE FROM controller_ssh_keys")
            con.execute("DELETE FROM host_runtime_observations")
            con.execute("DELETE FROM cluster_zoning_observations")
            con.execute("DELETE FROM cluster_assignments")
            con.execute("DELETE FROM memberships")
            con.execute("DELETE FROM clusters")
            con.execute("DELETE FROM nodes")

    def login(self):
        response = self.client.post("/api/auth/login", json={"username": "operator", "password": "test-password"})
        self.assertEqual(response.status_code, 200)
        return {"Authorization": "Bearer " + response.json()["token"]}

    def node(self, headers):
        response = self.client.post("/api/nodes", headers=headers, json={
            "name": "node-a", "address": "192.0.2.102", "ssh_port": 22, "ssh_user": "root", "enabled": True,
        })
        self.assertEqual(response.status_code, 201)
        return response.json()["id"]

    def cluster(self, headers):
        response = self.client.post("/api/clusters", headers=headers, json={
            "name": "lab-a",
            "ports": {"elasticsearch_http": 9200, "elasticsearch_transport": 9300, "kibana": 5601, "fleet": 8220, "logstash_api": 9600},
        })
        self.assertEqual(response.status_code, 201)
        return response.json()["id"]

    def test_cluster_defaults_and_structured_settings(self):
        headers = self.login()
        cluster_id = self.cluster(headers)
        cluster = self.client.get(f"/api/clusters/{cluster_id}", headers=headers).json()
        self.assertRegex(cluster["theme_color"], r"^#[0-9A-F]{6}$")
        self.assertEqual(cluster["desired_version"], self.main.DEFAULT_STACK_VERSION)
        self.assertEqual(cluster["network_defaults"]["mode"], "shared")

        invalid = self.client.put(f"/api/clusters/{cluster_id}/settings", headers=headers, json={
            "allocation_enable": "invalid",
        })
        self.assertEqual(invalid.status_code, 422)

        response = self.client.put(f"/api/clusters/{cluster_id}/settings", headers=headers, json={
            "allocation_enable": "primaries",
            "rebalance_enable": "all",
            "disk_watermark_low": "80%",
            "disk_watermark_high": "90%",
            "disk_watermark_flood_stage": "95%",
            "recovery_max_bytes_per_sec": "80mb",
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn("run_id", response.json())
        stored = self.client.get(f"/api/clusters/{cluster_id}/settings", headers=headers).json()
        self.assertEqual(stored["elasticsearch_settings"]["allocation_enable"], "primaries")

    def test_host_runtime_and_deinitialize_guard(self):
        headers = self.login()
        node_id = self.node(headers)
        runtime = self.client.get(f"/api/nodes/{node_id}/runtime", headers=headers)
        self.assertEqual(runtime.status_code, 200)
        self.assertFalse(runtime.json()["initialized"])

        with patch.object(self.main, "launch", return_value=81):
            initialized = self.client.post(f"/api/nodes/{node_id}/initialize", headers=headers)
        self.assertEqual(initialized.json()["run_id"], 81)

        cluster_id = self.cluster(headers)
        self.client.post(f"/api/clusters/{cluster_id}/members", headers=headers, json={
            "node_id": node_id,
            "network_mode": "shared",
            "data_interface": "ens18",
            "data_address": "192.0.2.102",
            "user_interface": "ens18",
            "user_address": "192.0.2.102",
        })
        self.client.post(f"/api/clusters/{cluster_id}/assignments", headers=headers, json={
            "node_id": node_id,
            "role": "master",
            "config": {"cpu": "2", "memory": "4g", "storage_path": "/srv/elastic/lab-a/master"},
        })
        blocked = self.client.post(f"/api/nodes/{node_id}/deinitialize", headers=headers)
        self.assertEqual(blocked.status_code, 409)

    def test_host_reboot_creates_a_managed_run(self):
        headers = self.login()
        node_id = self.node(headers)
        with patch.object(self.main, "launch", return_value=82) as launch:
            rebooted = self.client.post(f"/api/nodes/{node_id}/reboot", headers=headers)
        self.assertEqual(rebooted.status_code, 200)
        self.assertEqual(rebooted.json()["run_id"], 82)
        self.assertEqual(launch.call_args.args[:2], ("host-reboot", "node-a"))

    def test_host_identity_reports_os_and_installed_podman_version(self):
        async def collect():
            with patch.object(self.main.console, "remote_command", new=AsyncMock(return_value=b"ECP_OS=Rocky Linux 9.6\nECP_PODMAN=podman version 5.8.5\n")):
                return await self.main.console.host_identity({"ssh_port": 22, "ssh_user": "root", "address": "192.0.2.102"})
        os_name, podman_version = asyncio.run(collect())
        self.assertEqual(os_name, "Rocky Linux 9.6")
        self.assertEqual(podman_version, "5.8.5")

    def test_ssh_authentication_failures_are_summarized_without_known_host_noise(self):
        error = "Warning: Permanently added '192.0.2.101' (ED25519) to the list of known hosts.\nroot@192.0.2.101: Permission denied (publickey,password)."
        self.assertEqual(self.main.console.ssh_error_summary(error), "Controller SSH key authentication failed")
        args = self.main.ssh_host_key_args({"ssh_host_key": "", "ssh_auth_state": "controller_key"}, "/tmp/known_hosts")
        self.assertIn("LogLevel=ERROR", args)

    def test_host_storage_inventory_exposes_only_safe_writable_mounts(self):
        headers = self.login()
        node_id = self.node(headers)
        inventory = {
            "filesystems": [{
                "target": "/", "source": "/dev/mapper/root", "fstype": "xfs", "options": "rw,relatime", "size": 1000, "avail": 600,
                "children": [
                    {"target": "/srv/elastic", "source": "/dev/sdb1", "fstype": "xfs", "options": "rw,noatime", "size": 2000, "avail": 1500},
                    {"target": "/boot", "source": "/dev/sda1", "fstype": "xfs", "options": "rw,relatime", "size": 200, "avail": 100},
                    {"target": "/var/lib/containers/storage", "source": "/dev/mapper/root[/var/lib/containers/storage]", "fstype": "xfs", "options": "rw,relatime", "size": 1000, "avail": 600},
                    {"target": "/mnt/archive", "source": "/dev/sdc1", "fstype": "ext4", "options": "ro,relatime", "size": 3000, "avail": 2100},
                ],
            }],
        }
        with patch.object(self.main.console, "remote_command", new=AsyncMock(return_value=json.dumps(inventory).encode())):
            response = self.client.get(f"/api/nodes/{node_id}/storage", headers=headers)
        self.assertEqual(response.status_code, 200)
        mounts = {item["mount_point"]: item for item in response.json()["mounts"]}
        self.assertTrue(mounts["/srv/elastic"]["eligible"])
        self.assertEqual(mounts["/srv/elastic"]["source"], "/dev/sdb1")
        self.assertEqual(mounts["/srv/elastic"]["available_bytes"], 1500)
        self.assertTrue(mounts["/"]["eligible"])
        self.assertFalse(mounts["/boot"]["eligible"])
        self.assertEqual(mounts["/var/lib/containers/storage"]["unavailable_reason"], "controller-reserved mount")
        self.assertEqual(mounts["/mnt/archive"]["unavailable_reason"], "read-only mount")
        self.assertNotIn("options", mounts["/srv/elastic"])

    def test_sensitive_values_require_scoped_reauthentication(self):
        headers = self.login()
        cluster_id = self.cluster(headers)
        items = self.client.get(f"/api/clusters/{cluster_id}/sensitive-items", headers=headers).json()["items"]
        password_item = next(item for item in items if item["id"] == "cluster.elastic_password")
        self.assertEqual(password_item["masked_value"], "********")
        self.assertNotIn("value", password_item)

        rejected = self.client.post("/api/auth/reveal-grants", headers=headers, json={
            "cluster_id": cluster_id, "password": "wrong",
        })
        self.assertEqual(rejected.status_code, 401)

        grant = self.client.post("/api/auth/reveal-grants", headers=headers, json={
            "cluster_id": cluster_id, "password": "test-password",
        }).json()["grant_token"]
        revealed = self.client.post(
            f"/api/clusters/{cluster_id}/sensitive-items/cluster.elastic_password/reveal",
            headers=headers,
            json={"grant_token": grant, "purpose": "copy"},
        )
        self.assertEqual(revealed.status_code, 200)
        self.assertGreater(len(revealed.json()["value"]), 20)
        with self.main.db() as con:
            audit = con.execute("SELECT action,item_id FROM audit_events ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual((audit["action"], audit["item_id"]), ("copy", "cluster.elastic_password"))

    def test_sensitive_certificate_and_key_paths_are_visible_without_values(self):
        headers = self.login()
        node_id = self.node(headers)
        cluster_id = self.cluster(headers)
        self.client.post(f"/api/clusters/{cluster_id}/members", headers=headers, json={
            "node_id": node_id,
            "network_mode": "shared",
            "data_interface": "ens18",
            "data_address": "192.0.2.102",
            "user_interface": "ens18",
            "user_address": "192.0.2.102",
        })
        self.client.post(f"/api/clusters/{cluster_id}/assignments", headers=headers, json={
            "node_id": node_id,
            "role": "master",
            "config": {"cpu": "2", "memory": "4g", "storage_path": "/srv/elastic/lab-a/master"},
        })
        with patch.object(self.main.console, "remote_sensitive_metadata", new=AsyncMock(side_effect=lambda item: {**item, "available": True})):
            items = self.client.get(f"/api/clusters/{cluster_id}/sensitive-items", headers=headers).json()["items"]
        certificate = next(item for item in items if item["id"] == "cluster.ca_certificate")
        private_key = next(item for item in items if item["id"] == "cluster.ca_private_key")
        credential = next(item for item in items if item["id"] == "cluster.elastic_password")
        self.assertEqual(certificate["storage_path"], "/etc/elastic-control/clusters/lab-a/ca/ca.crt")
        self.assertEqual(private_key["storage_path"], "/etc/elastic-control/clusters/lab-a/ca/ca.key")
        self.assertNotIn("storage_path", credential)
        self.assertNotIn("value", certificate)

    def test_dashboard_snapshot_isolated_failure_states(self):
        headers = self.login()
        node_id = self.node(headers)
        cluster_id = self.cluster(headers)
        self.main.console.telemetry.host_states[node_id] = {
            "node_id": node_id, "reachable": False, "podman_socket_active": False,
            "last_error": "SSH connection failed", "observed_at": "2026-07-31T12:00:00Z", "containers": [], "pods": [],
        }
        response = self.client.get("/api/dashboard/snapshot", headers=headers)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        cluster = next(item for item in payload["clusters"] if item["id"] == cluster_id)
        self.assertEqual(cluster["health"], "unknown")
        self.assertFalse(payload["hosts"][0]["reachable"])
        self.assertTrue(any(alert["source"] == "host" for alert in payload["alerts"]))

    def test_dashboard_reports_master_only_cluster_as_awaiting_data(self):
        headers = self.login()
        node_id = self.node(headers)
        cluster_id = self.cluster(headers)
        self.assertEqual(self.client.post(f"/api/clusters/{cluster_id}/members", headers=headers, json={
            "node_id": node_id,
            "network_mode": "shared",
            "data_interface": "ens18",
            "data_address": "192.0.2.102",
            "user_interface": "ens18",
            "user_address": "192.0.2.102",
        }).status_code, 201)
        self.assertEqual(self.client.post(f"/api/clusters/{cluster_id}/assignments", headers=headers, json={
            "node_id": node_id,
            "role": "master",
            "config": {"cpu": "2", "memory": "4g", "storage_path": "/srv/elastic/lab-a/master"},
        }).status_code, 201)

        payload = self.client.get("/api/dashboard/snapshot", headers=headers).json()
        cluster = next(item for item in payload["clusters"] if item["id"] == cluster_id)
        self.assertEqual(cluster["health"], "awaiting_data")
        self.assertEqual(cluster["metrics"]["status"], "awaiting_data")
        self.assertFalse(any(alert["source"] == "cluster" and alert["source_id"] == cluster_id for alert in payload["alerts"]))

    def test_dashboard_replaces_a_rejected_monitoring_key_once(self):
        headers = self.login()
        node_id = self.node(headers)
        cluster_id = self.cluster(headers)
        self.assertEqual(self.client.post(f"/api/clusters/{cluster_id}/members", headers=headers, json={
            "node_id": node_id,
            "network_mode": "shared",
            "data_interface": "ens18",
            "data_address": "192.0.2.102",
            "user_interface": "ens18",
            "user_address": "192.0.2.102",
        }).status_code, 201)
        for role in ("master", "hot"):
            self.assertEqual(self.client.post(f"/api/clusters/{cluster_id}/assignments", headers=headers, json={
                "node_id": node_id,
                "role": role,
                "config": {"cpu": "2", "memory": "4g", "storage_path": f"/srv/elastic/lab-a/{role}"},
            }).status_code, 201)

        role_ports = self.main.default_role_ports()
        role_ports["master"] = {"elasticsearch_http": 9210, "elasticsearch_transport": 9310}
        with self.main.db() as con:
            credentials = self.main.open_config(con.execute("SELECT secrets_json FROM clusters WHERE id=?", (cluster_id,)).fetchone()["secrets_json"])
            credentials["monitoring_api_key"] = "stale-key"
            con.execute(
                "UPDATE clusters SET role_ports_json=?,secrets_json=? WHERE id=?",
                (json.dumps(role_ports, sort_keys=True), self.main.seal_config(json.dumps(credentials)), cluster_id),
            )

        console = self.main.console

        class Response:
            def __init__(self, payload, status_code=200):
                self.payload = payload
                self.status_code = status_code

            def raise_for_status(self):
                if self.status_code >= 400:
                    request = console.httpx.Request("GET", "https://cluster.invalid")
                    response = console.httpx.Response(self.status_code, request=request)
                    raise console.httpx.HTTPStatusError("Client error", request=request, response=response)

            def json(self):
                return self.payload

        class Client:
            instances = []
            get_calls = []
            post_calls = []

            def __init__(self, **kwargs):
                self.kwargs = kwargs
                Client.instances.append(self)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return False

            async def post(self, path, **kwargs):
                Client.post_calls.append((path, kwargs))
                return Response({"id": "replacement-id", "api_key": "replacement-key"})

            async def get(self, path, **kwargs):
                Client.get_calls.append((path, kwargs))
                if kwargs["headers"]["Authorization"] == "ApiKey stale-key":
                    return Response({}, 401)
                payloads = {
                    "/_cluster/health?level=indices": {"status": "green", "number_of_nodes": 1, "number_of_data_nodes": 1, "indices": {}},
                    "/_cluster/stats": {"indices": {"count": 1, "docs": {"count": 2}, "store": {"size_in_bytes": 3}}},
                    "/_nodes/stats/fs,jvm,process,os": {"nodes": {}},
                    "/_cluster/pending_tasks": {"tasks": []},
                    "/_cluster/settings?include_defaults=true&flat_settings=true": {},
                    "/_cat/allocation?format=json&bytes=b&h=node,shards": [],
                }
                return Response(payloads[path])

        manager = self.main.console.TelemetryManager()
        with patch.object(manager, "_ensure_cluster_ca", new=AsyncMock(return_value="/tmp/cluster-ca.crt")), \
             patch.object(self.main.console, "ca_ssl_context", return_value=object()), \
             patch.object(self.main.console.httpx, "AsyncClient", Client):
            asyncio.run(manager._collect_cluster(cluster_id))

        state = manager.cluster_states[cluster_id]
        replacement = "ApiKey " + self.main.console.base64.b64encode(b"replacement-id:replacement-key").decode()
        self.assertEqual(state["status"], "green", state)
        self.assertEqual(state["last_error"], "")
        self.assertEqual(Client.instances[0].kwargs["base_url"], "https://192.0.2.102:9210")
        self.assertEqual(len(Client.post_calls), 1)
        self.assertTrue(any(call[1]["headers"]["Authorization"] == "ApiKey stale-key" for call in Client.get_calls))
        self.assertTrue(any(call[1]["headers"]["Authorization"] == replacement for call in Client.get_calls))
        with self.main.db() as con:
            stored = self.main.open_config(con.execute("SELECT secrets_json FROM clusters WHERE id=?", (cluster_id,)).fetchone()["secrets_json"])
        self.assertEqual(stored["monitoring_api_key"], replacement.removeprefix("ApiKey "))

    def test_node_breakdown_classifies_tiers_and_combines_allocation(self):
        breakdown = self.main.console.node_breakdown({
            "hot-id": {
                "name": "hot-1", "roles": ["master", "data_hot"],
                "attributes": {"zone": "zone-a"},
                "fs": {"total": {"total_in_bytes": 1000, "available_in_bytes": 400}},
                "jvm": {"mem": {"heap_used_in_bytes": 300, "heap_max_in_bytes": 600}},
            },
            "warm-id": {
                "name": "warm-1", "roles": ["data_warm"],
                "attributes": {"zone": "zone-b"},
                "fs": {"total": {"total_in_bytes": 2000, "available_in_bytes": 1200}},
                "jvm": {"mem": {"heap_used_in_bytes": 200, "heap_max_in_bytes": 800}},
            },
            "other-id": {
                "name": "ingest-1", "roles": ["ingest"],
                "fs": {"total": {"total_in_bytes": 500, "available_in_bytes": 300}},
                "jvm": {"mem": {"heap_used_in_bytes": 100, "heap_max_in_bytes": 400}},
            },
        }, [{"node": "warm-1", "shards": "4"}, {"node": "hot-1", "shards": "7"}])
        self.assertEqual([item["node_type"] for item in breakdown], ["Hot data", "Warm data", "Ingest"])
        self.assertEqual([item["shards"] for item in breakdown], [7, 4, 0])
        self.assertEqual(breakdown[0]["disk_used_bytes"], 600)
        self.assertEqual(breakdown[1]["heap_max_bytes"], 800)
        self.assertEqual([item["zone"] for item in breakdown], ["zone-a", "zone-b", ""])
        zones = self.main.console.zone_breakdown(breakdown)
        self.assertEqual([(item["zone"], item["nodes"], item["shards"]) for item in zones], [
            ("zone-a", 1, 7), ("zone-b", 1, 4), ("unassigned", 1, 0),
        ])

    def test_runtime_zoning_observation_records_drift_and_dashboard_alert(self):
        headers = self.login()
        node_id = self.node(headers)
        cluster_id = self.cluster(headers)
        with self.main.db() as con:
            con.execute("UPDATE nodes SET zone_id='zone-a' WHERE id=?", (node_id,))
            con.execute(
                "UPDATE clusters SET zoning_json=? WHERE id=?",
                (json.dumps({"mode": "awareness", "zones": ["zone-a", "zone-b"]}), cluster_id),
            )
            con.execute(
                "INSERT INTO memberships(cluster_id,node_id,network_mode,data_interface,data_address,user_interface,user_address) VALUES (?,?, 'shared','ens18','192.0.2.102','ens18','192.0.2.102')",
                (cluster_id, node_id),
            )
            assignment_id = con.execute(
                "INSERT INTO cluster_assignments(cluster_id,node_id,role,config_json,state) VALUES (?,?, 'master','{}','active')",
                (cluster_id, node_id),
            ).lastrowid
            con.execute(
                "INSERT INTO cluster_zoning_observations(cluster_id,applied_mode,applied_zones_json,status) VALUES (?,'awareness',?,'applied')",
                (cluster_id, json.dumps(["zone-a", "zone-b"])),
            )
            cluster = self.main.cluster_record(con, cluster_id)

        manager = self.main.console.TelemetryManager()
        manager._record_cluster_zoning(cluster, [{
            "id": "runtime-master", "name": f"ecp-lab-a-master-{node_id}", "zone": "zone-b",
            "node_type": "Master", "roles": ["master"], "shards": 0,
            "disk_total_bytes": 0, "disk_available_bytes": 0, "disk_used_bytes": 0,
            "heap_used_bytes": 0, "heap_max_bytes": 0,
        }])

        with self.main.db() as con:
            observation = dict(con.execute("SELECT * FROM cluster_zoning_observations WHERE cluster_id=?", (cluster_id,)).fetchone())
        self.assertEqual(observation["status"], "drift")
        self.assertEqual(json.loads(observation["observed_zones_json"]), {str(assignment_id): "zone-b"})
        snapshot = manager.snapshot()
        self.assertTrue(any("zone drift" in alert["message"].lower() for alert in snapshot["alerts"]))

    def test_host_telemetry_separates_ssh_reachability_from_podman_state(self):
        headers = self.login()
        node_id = self.node(headers)
        with self.main.db() as con:
            node = dict(con.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone())

        manager = self.main.console.TelemetryManager()
        stale_tunnel = SimpleNamespace(close=AsyncMock())
        manager.tunnels[node_id] = stale_tunnel
        with patch.object(
            self.main.console,
            "remote_command",
            new=AsyncMock(return_value=b"uninitialized"),
        ):
            asyncio.run(manager._collect_host(node))

        state = manager.host_states[node_id]
        self.assertTrue(state["reachable"])
        self.assertFalse(state["initialized"])
        self.assertFalse(state["podman_socket_active"])
        self.assertEqual(state["last_error"], "")
        stale_tunnel.close.assert_awaited_once()

        podman_tunnel = SimpleNamespace(
            ensure=AsyncMock(side_effect=RuntimeError("socket unavailable")),
            close=AsyncMock(),
        )
        manager.tunnels[node_id] = podman_tunnel
        with patch.object(
            self.main.console,
            "remote_command",
            new=AsyncMock(return_value=b"initialized"),
        ):
            asyncio.run(manager._collect_host(node))

        state = manager.host_states[node_id]
        self.assertTrue(state["reachable"])
        self.assertTrue(state["initialized"])
        self.assertFalse(state["podman_socket_active"])
        self.assertEqual(state["last_error"], "Podman: socket unavailable")
        podman_tunnel.close.assert_awaited_once()

    def test_host_telemetry_persists_actual_workload_image_version(self):
        headers = self.login()
        node_id = self.node(headers)
        cluster_id = self.cluster(headers)
        with self.main.db() as con:
            assignment_id = con.execute(
                "INSERT INTO cluster_assignments(cluster_id,node_id,role,config_json,image_version,state) VALUES (?,?,?,?,?,'active')",
                (cluster_id, node_id, "master", "{}", "9.9.9"),
            ).lastrowid

        manager = self.main.console.TelemetryManager()
        manager._record_workload_runtime(node_id, [{
            "name": f"ecp-lab-a-master-{node_id}",
            "image": "docker.elastic.co/elasticsearch/elasticsearch:8.19.1",
            "digest": "sha256:runtime-image",
            "state": "running",
            "status": "Up 1 minute",
        }])

        with self.main.db() as con:
            observation = dict(con.execute(
                "SELECT image,digest,version,running,cached,error FROM workload_observations WHERE assignment_id=?",
                (assignment_id,),
            ).fetchone())
        self.assertEqual(observation["version"], "8.19.1")
        self.assertEqual(observation["image"], "docker.elastic.co/elasticsearch/elasticsearch:8.19.1")
        self.assertEqual(observation["digest"], "sha256:runtime-image")
        self.assertEqual(observation["running"], 1)
        self.assertEqual(observation["cached"], 1)
        self.assertEqual(observation["error"], "")

    def test_dashboard_stream_requires_a_scoped_token(self):
        headers = self.login()
        token = self.client.post("/api/dashboard/stream-token", headers=headers).json()["token"]
        self.assertEqual(self.client.get("/api/dashboard/events?token=invalid").status_code, 401)
        self.assertTrue(self.main.console.valid_scope_token(token, "dashboard"))
        self.assertFalse(self.main.console.valid_scope_token(token, "another-scope"))

    def test_ca_context_keeps_verification_and_accepts_legacy_controller_cas(self):
        context = SimpleNamespace(verify_flags=ssl.VERIFY_X509_STRICT | 4)
        with patch.object(self.main.console.ssl, "create_default_context", return_value=context) as create:
            result = self.main.console.ca_ssl_context("/tmp/cluster-ca.crt")
        self.assertIs(result, context)
        create.assert_called_once_with(cafile="/tmp/cluster-ca.crt")
        self.assertFalse(context.verify_flags & ssl.VERIFY_X509_STRICT)
        self.assertTrue(context.verify_flags & 4)

    def test_cluster_deletion_invalidates_cached_ca(self):
        headers = self.login()
        cluster_id = self.cluster(headers)
        cached_ca = self.main.console.CA_CACHE / f"cluster-{cluster_id}.crt"
        cached_ca.parent.mkdir(parents=True, exist_ok=True)
        cached_ca.write_text("stale")
        self.assertEqual(self.client.delete(f"/api/clusters/{cluster_id}", headers=headers).status_code, 204)
        self.assertFalse(cached_ca.exists())

    def test_host_lifecycle_and_workload_identity_playbooks(self):
        playbooks = self.main.PLAYBOOKS
        host_init = (playbooks / "host-init.yml").read_text()
        host_deinit = (playbooks / "host-deinit.yml").read_text()
        host_reboot = (playbooks / "host-reboot.yml").read_text()
        reconcile = (playbooks / "cluster-reconcile.yml").read_text()
        settings = (playbooks / "cluster-settings.yml").read_text()
        zoning = (playbooks / "cluster-zoning-settings.yml").read_text()
        self.assertIn("podman.socket", host_init)
        self.assertIn("elastic-control-host-init", host_init)
        self.assertIn("SELINUX=disabled", host_init)
        self.assertIn("setenforce 0", host_init)
        self.assertIn("Verify SELinux is non-enforcing", host_init)
        self.assertNotIn("ansible.builtin.reboot", host_init)
        self.assertIn("Refuse de-initialization while managed workloads exist", host_deinit)
        self.assertIn("state: stopped", host_deinit)
        self.assertIn("Remove the inactive Podman socket path", host_deinit)
        self.assertIn("Reboot managed Elastic host", host_reboot)
        self.assertIn("ansible.builtin.reboot", host_reboot)
        self.assertIn("Verify host is reachable after reboot", host_reboot)
        self.assertIn("io.elastic-control.assignment-id", reconcile)
        self.assertIn("io.elastic-control.cluster-slug", reconcile)
        self.assertIn("coordinating: \"[]\"", reconcile)
        self.assertIn("_cluster/settings", settings)
        self.assertIn("cluster.routing.allocation.disk.watermark.flood_stage", settings)
        self.assertIn("ES_SETTING_NODE_ATTR_ZONE={{ membership.zone_id }}", reconcile)
        self.assertIn("cluster.routing.allocation.awareness.attributes", zoning)
        self.assertIn("cluster.routing.allocation.awareness.force.zone.values", zoning)
        self.assertIn("Restore previous zoning settings", zoning)


if __name__ == "__main__":
    unittest.main()
