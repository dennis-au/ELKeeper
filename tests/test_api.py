import asyncio
import importlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from email.message import Message

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from fastapi.testclient import TestClient


class ApiTests(unittest.TestCase):
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
            con.execute("DELETE FROM controller_settings")
            con.execute("DELETE FROM controller_ssh_keys")
            con.execute("DELETE FROM workload_change_batches")
            con.execute("DELETE FROM cluster_zoning_observations")
            # Plans deliberately hold immutable references to their workload
            # targets. Clear the isolated test plans before resetting those
            # targets so one planning test cannot affect the next API case.
            con.execute("DELETE FROM maintenance_plans")
            con.execute("UPDATE runs SET status='failed', finished_at=CURRENT_TIMESTAMP WHERE status IN ('queued','running','recovery_required')")
            con.execute("DELETE FROM cluster_assignments")
            con.execute("DELETE FROM memberships")
            con.execute("DELETE FROM clusters")
            con.execute("DELETE FROM nodes")

    def login(self):
        result = self.client.post("/api/auth/login", json={"username": "operator", "password": "test-password"})
        self.assertEqual(result.status_code, 200)
        return {"Authorization": "Bearer " + result.json()["token"]}

    def node(self, headers, name="node-a", address="192.0.2.102"):
        result = self.client.post("/api/nodes", headers=headers, json={
            "name": name, "address": address, "ssh_port": 22, "ssh_user": "root", "enabled": True,
        })
        self.assertEqual(result.status_code, 201)
        return result.json()["id"]

    def cluster(self, headers, name="lab-a", ports=None):
        result = self.client.post("/api/clusters", headers=headers, json={
            "name": name,
            "ports": ports or {"elasticsearch_http": 9200, "elasticsearch_transport": 9300, "kibana": 5601, "fleet": 8220, "logstash_api": 9600},
        })
        self.assertEqual(result.status_code, 201)
        return result.json()["id"]

    def membership(self, headers, cluster_id, node_id, user_address="192.0.2.102", data_address="198.51.100.102"):
        return self.client.post(f"/api/clusters/{cluster_id}/members", headers=headers, json={
            "node_id": node_id,
            "data_interface": "ens19",
            "data_address": data_address,
            "user_interface": "ens18",
            "user_address": user_address,
        })

    def frontend_markup_and_source(self):
        """Read source files in a checkout and compiled assets in the runtime image."""
        root = Path(__file__).resolve().parents[1]
        frontend = root / "frontend"
        if (frontend / "index.html").is_file() and (frontend / "src").is_dir():
            markup = (frontend / "index.html").read_text()
            source_files = frontend.joinpath("src").rglob("*.ts*")
        else:
            frontend = root / "static"
            markup = (frontend / "index.html").read_text()
            source_files = frontend.joinpath("assets").rglob("*.js")
        return markup, "\n".join(path.read_text() for path in sorted(source_files))

    def test_login_rejects_bad_password(self):
        self.assertEqual(self.client.post("/api/auth/login", json={"username": "operator", "password": "wrong"}).status_code, 401)

    def test_relative_data_dir_uses_the_persistent_mount_when_available(self):
        with patch.object(self.main, "PERSISTENT_DATA_DIR", Path(self.temp.name)), patch.dict(os.environ, {"APP_DATA_DIR": "./data"}):
            self.assertEqual(self.main.app_data_dir(), Path(self.temp.name))

    def test_available_versions_accepts_assignments_without_observations(self):
        assignments = [{"role": "hot", "observation": None, "image_version": "8.16.0", "desired_version": "8.16.0"}]
        with patch.object(self.main, "registry_tags", return_value={"8.16.0"}):
            self.assertEqual(self.main.available_versions(assignments), ["8.16.0"])

    def test_workload_version_recommendation_prefers_the_running_cluster_version(self):
        assignments = [
            {"role": "master", "observation": {"running": True, "version": "8.19.0"}, "image_version": "8.18.0"},
            {"role": "hot", "observation": None, "image_version": "8.19.0"},
        ]
        self.assertEqual(self.main.recommended_workload_version(assignments, ["8.20.0", "8.19.0"]), "8.19.0")
        self.assertEqual(self.main.recommended_workload_version(
            [{"role": "master", "observation": None, "desired_version": "8.19.0"}],
            ["8.20.0", "8.19.0"],
        ), "8.19.0")
        self.assertEqual(self.main.recommended_workload_version([], ["8.20.0", "8.19.0"]), "8.20.0")

    def test_role_aware_versions_work_before_the_cluster_has_assignments(self):
        headers = self.login()
        cluster_id = self.cluster(headers)
        with patch.object(self.main, "available_role_versions", return_value=["8.20.0", "8.19.0"]) as available:
            result = self.client.get(f"/api/clusters/{cluster_id}/versions?role=kibana", headers=headers)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["recommended_version"], "8.20.0")
        self.assertEqual(available.call_args.args[0], "kibana")

    def test_role_version_discovery_scans_only_the_selected_workload_image(self):
        assignments = [{"role": "master", "image_version": "8.19.0", "observation": None}]
        with patch.object(self.main, "registry_listing_tags", return_value={"8.20.0", "8.19.0"}) as registry:
            versions = self.main.available_role_versions("kibana", assignments)
        self.assertEqual(versions, ["8.20.0", "8.19.0"])
        self.assertEqual(registry.call_args.args[0], "kibana/kibana")

    def test_role_version_discovery_keeps_the_current_version_beside_newer_releases(self):
        assignments = [{"role": "master", "image_version": "8.19.0", "observation": None}]
        releases = {f"9.4.{patch}" for patch in range(11)} | {"8.19.0", "8.18.0"}
        with patch.object(self.main, "registry_listing_tags", return_value=releases):
            versions = self.main.available_role_versions("master", assignments)
        self.assertEqual(len(versions), self.main.REGISTRY_TAG_RESULT_LIMIT + 1)
        self.assertIn("8.19.0", versions)
        self.assertNotIn("8.18.0", versions)

    def test_log_monitoring_defaults_migrate_safely_and_start_a_tracked_reconcile(self):
        headers = self.login()
        cluster_id = self.cluster(headers)
        created = self.client.get(f"/api/clusters/{cluster_id}", headers=headers).json()
        self.assertTrue(created["log_monitoring"]["filebeat_enabled"])
        self.assertEqual(created["log_monitoring"]["retention_days"], 30)
        with self.main.db() as con:
            encrypted = con.execute("SELECT secrets_json FROM clusters WHERE id=?", (cluster_id,)).fetchone()["secrets_json"]
        credentials = self.main.open_config(encrypted)
        self.assertTrue(credentials["filebeat_password"])
        self.assertNotIn(credentials["filebeat_password"], encrypted)

        with self.main.db() as con:
            con.execute("UPDATE clusters SET observability_json='{}' WHERE id=?", (cluster_id,))
        self.main.init()
        migrated = self.client.get(f"/api/clusters/{cluster_id}", headers=headers).json()
        self.assertFalse(migrated["log_monitoring"]["filebeat_enabled"])

        with patch.object(self.main, "launch_filebeat_reconcile", return_value=913) as launch:
            response = self.client.put(
                f"/api/clusters/{cluster_id}/log-monitoring",
                headers=headers,
                json={"filebeat_enabled": True},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"run_id": 913})
        launch.assert_called_once_with(cluster_id, "operator")

    def test_filebeat_companion_playbook_is_scoped_and_redacts_credentials(self):
        playbook = Path(self.main.PLAYBOOKS / "filebeat-reconcile.yml").read_text()
        workload_playbook = Path(self.main.PLAYBOOKS / "cluster-reconcile.yml").read_text()
        self.assertIn("podman inspect --format", playbook)
        self.assertIn("HostConfig.LogConfig.Path", playbook)
        self.assertIn("/var/log/elkeeper/workload.log:ro,Z", playbook)
        self.assertIn("logs-elkeeper.", playbook)
        self.assertIn("elkeeper-filebeat-30d", playbook)
        self.assertIn("io.elastic-control.role=filebeat", playbook)
        self.assertIn("labels.elkeeper_assignment_id", playbook)
        self.assertIn("User=0", playbook)
        self.assertIn('pipeline: "_none"', playbook)
        self.assertIn("decode_json_fields", playbook)
        self.assertIn("target: service", playbook)
        self.assertIn("type: elasticsearch", playbook)
        self.assertIn("/api/data_views/data_view", playbook)
        self.assertIn("elkeeper-logs-{{ cluster.slug }}", playbook)
        self.assertIn("no_log: true", playbook)
        self.assertIn("mode: '0600'", playbook)
        self.assertIn('monitoring.ui.logs.index: "logs-elkeeper.*"', workload_playbook)
        self.assertEqual(workload_playbook.count("LogDriver=k8s-file"), 5)

    def test_password_test_is_ephemeral_and_redacts_the_password(self):
        headers = self.login()
        commands = []

        def password_test(node, password):
            commands.append((node, password))
            return True, "Password authentication succeeded."

        before_inventory = set(self.main.INVENTORIES.glob("password-test-*"))
        before_variables = set(self.main.VARIABLES.glob("password-test-*"))
        with patch.object(self.main, "verify_ssh_password", side_effect=password_test):
            result = self.client.post("/api/nodes/test-password", headers=headers, json={
                "address": "192.0.2.101", "ssh_port": 22, "ssh_user": "root", "password": "one-time-secret",
            })
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json(), {"authenticated": True, "message": "Password authentication succeeded."})
        self.assertEqual(commands[0][0]["address"], "192.0.2.101")
        self.assertEqual(commands[0][1], "one-time-secret")
        self.assertEqual(before_inventory, set(self.main.INVENTORIES.glob("password-test-*")))
        self.assertEqual(before_variables, set(self.main.VARIABLES.glob("password-test-*")))
        with self.main.db() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM nodes").fetchone()[0], 0)
            audit = con.execute("SELECT action,detail FROM audit_events ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual((audit["action"], audit["detail"]), ("host_password_test", "succeeded"))

    def test_password_test_forces_a_fresh_password_only_ssh_connection(self):
        node = {
            "address": "192.0.2.101", "ssh_port": 22, "ssh_user": "root",
            "ssh_host_key": "", "ssh_auth_state": "pending",
        }
        command = self.main.password_test_command(node, 17, None)
        self.assertEqual(command[:4], ["sshpass", "-d", "17", "ssh"])
        self.assertIn("ControlMaster=no", command)
        self.assertIn("ControlPath=none", command)
        self.assertIn("PubkeyAuthentication=no", command)
        self.assertIn("PasswordAuthentication=yes", command)
        self.assertIn("root@192.0.2.101", command)
        self.assertNotIn("one-time-secret", command)

    def test_ansible_config_does_not_override_per_host_ssh_host_key_policy(self):
        config = (self.main.SOURCE_ROOT / "ansible" / "ansible.cfg").read_text()
        self.assertNotIn("ssh_args =", config)
        self.assertIn("pipelining = True", config)

    def test_legacy_known_hosts_removal_is_per_host_and_audited(self):
        headers = self.login()
        node_id = self.node(headers)
        with self.main.db() as con:
            legacy = dict(con.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone())
        self.assertTrue(self.main.host_key_validation_enabled(legacy))

        removed = self.client.post(f"/api/nodes/{node_id}/legacy-known-hosts/remove", headers=headers)
        self.assertEqual(removed.status_code, 200)
        self.assertEqual(removed.json(), {"updated": True, "legacy_known_hosts_disabled": True})

        with self.main.db() as con:
            node = dict(con.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone())
            audit = con.execute("SELECT action,detail FROM audit_events ORDER BY id DESC LIMIT 1").fetchone()
        self.assertTrue(node["legacy_known_hosts_disabled"])
        self.assertFalse(self.main.host_key_validation_enabled(node))
        self.assertEqual((audit["action"], audit["detail"]), ("host_legacy_known_hosts_removed", "legacy host-key trust disabled for this host"))

    def test_controller_display_timezone_is_validated_persisted_and_audited(self):
        headers = self.login()
        default = self.client.get("/api/controller/settings", headers=headers)
        self.assertEqual(default.status_code, 200)
        self.assertEqual(default.json()["timezone"], self.main.DEFAULT_DISPLAY_TIMEZONE)
        invalid = self.client.put("/api/controller/settings", headers=headers, json={"timezone": "invalid/timezone"})
        self.assertEqual(invalid.status_code, 422)
        updated = self.client.put("/api/controller/settings", headers=headers, json={"timezone": "America/New_York"})
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["timezone"], "America/New_York")
        with self.main.db() as con:
            audit = con.execute("SELECT action,detail FROM audit_events ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual((audit["action"], audit["detail"]), ("controller_display_timezone_updated", "America/New_York"))

    def test_controller_key_metadata_is_redacted_and_enrollment_pins_host_key(self):
        headers = self.login()
        private = ed25519.Ed25519PrivateKey.generate()
        with patch.object(self.main, "secure_transport"):
            generated = self.client.post("/api/controller/ssh-key/generate", headers=headers, json={"password": "test-password"})
        self.assertEqual(generated.status_code, 200)
        active = generated.json()["status"]["active"]
        self.assertEqual(active["source"], "generated")
        self.assertTrue(active["key_id"].startswith("SHA256:"))
        self.assertNotIn("private", generated.text.lower())
        with self.main.db() as con:
            stored = con.execute("SELECT private_key_encrypted FROM controller_ssh_keys").fetchone()["private_key_encrypted"]
        self.assertNotIn("BEGIN OPENSSH PRIVATE KEY", stored)

        host_key = private.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()
        with patch.object(self.main, "launch_key_enrollment_probe", return_value=77) as launch:
            enrolled = self.client.post("/api/nodes/enroll", headers=headers, json={
                "name": "node-key", "address": "192.0.2.101", "ssh_port": 22, "ssh_user": "root", "enabled": True,
                "ssh_host_key": host_key, "auth_method": "controller_key", "install_controller_key": True,
            })
        self.assertEqual(enrolled.status_code, 201)
        self.assertEqual(enrolled.json()["run_id"], 77)
        self.assertEqual(launch.call_count, 1)
        nodes = self.client.get("/api/nodes", headers=headers).json()
        self.assertEqual(nodes[0]["ssh_auth_state"], "pending")
        self.assertEqual(nodes[0]["ssh_host_key"], host_key)

    def test_password_bootstrap_allows_http_and_unpinned_hosts(self):
        headers = self.login()
        self.main.stage_controller_key(ed25519.Ed25519PrivateKey.generate(), "generated")
        with patch.object(self.main, "launch_password_enrollment", return_value=88) as launch:
            enrolled = self.client.post("/api/nodes/enroll", headers=headers, json={
                "name": "node-password", "address": "192.0.2.101", "ssh_port": 22, "ssh_user": "root", "enabled": True,
                "auth_method": "password", "password": "not-stored", "install_controller_key": True,
            })
        self.assertEqual(enrolled.status_code, 201)
        self.assertEqual(enrolled.json()["run_id"], 88)
        self.assertEqual(launch.call_args.args[1], "not-stored")
        with self.main.db() as con:
            node = con.execute("SELECT ssh_host_key FROM nodes").fetchone()
        self.assertEqual(node["ssh_host_key"], "")

    def test_candidate_key_probe_uses_the_staged_private_key(self):
        headers = self.login()
        with patch.object(self.main, "secure_transport"):
            self.assertEqual(self.client.post("/api/controller/ssh-key/generate", headers=headers, json={"password": "test-password"}).status_code, 200)
        node_id = self.node(headers)
        with patch.object(self.main, "secure_transport"):
            staged = self.client.post("/api/controller/ssh-key/generate", headers=headers, json={"password": "test-password"})
        self.assertEqual(staged.status_code, 200)
        candidate_id = staged.json()["key"]["key_id"]
        with self.main.db() as con:
            node = dict(con.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone())
        with patch.object(self.main, "launch", return_value=77) as launch:
            self.assertEqual(self.main.launch_key_enrollment_probe(node, "operator"), 77)
        key_path = launch.call_args.kwargs["private_key"]
        self.assertIn(self.main.re.sub(r"[^A-Za-z0-9._-]", "_", candidate_id), key_path)
        command = launch.call_args.args[2]("inventory", None)
        self.assertIn(key_path, command)

    def test_candidate_activation_and_replacement_require_verified_host_state(self):
        headers = self.login()
        with patch.object(self.main, "secure_transport"):
            self.assertEqual(self.client.post("/api/controller/ssh-key/generate", headers=headers, json={"password": "test-password"}).status_code, 200)
        node_id = self.node(headers)
        with patch.object(self.main, "secure_transport"):
            staged = self.client.post("/api/controller/ssh-key/generate", headers=headers, json={"password": "test-password"})
            blocked = self.client.post("/api/controller/ssh-key/activate", headers=headers, json={"password": "test-password"})
        self.assertEqual(staged.status_code, 200)
        self.assertEqual(blocked.status_code, 409)
        candidate_id = staged.json()["key"]["key_id"]
        with self.main.db() as con:
            con.execute("UPDATE nodes SET candidate_key_id=?,ssh_auth_state='candidate_ready' WHERE id=?", (candidate_id, node_id))
        with self.assertRaises(self.main.HTTPException) as replacement:
            self.main.stage_controller_key(ed25519.Ed25519PrivateKey.generate(), "generated")
        self.assertEqual(replacement.exception.status_code, 409)

    def test_password_bootstrap_inventory_forbids_key_authentication_and_allows_unpinned_hosts(self):
        headers = self.login()
        node_id = self.node(headers)
        with self.main.db() as con:
            con.execute("UPDATE nodes SET ssh_auth_state='pending' WHERE id=?", (node_id,))
        path = self.main.inventory(71, node_ids=[node_id], password_bootstrap=True)
        rendered = path.read_text()
        self.assertNotIn("ansible_ssh_private_key_file", rendered)
        self.assertIn("PubkeyAuthentication=no", rendered)
        self.assertIn("ControlMaster=no", rendered)
        self.assertIn("ControlPath=none", rendered)
        self.assertIn("StrictHostKeyChecking=no", rendered)
        self.assertIn("UserKnownHostsFile=/dev/null", rendered)
        path.unlink()
        playbook = (self.main.PLAYBOOKS / "host-bootstrap-key.yml").read_text()
        self.assertIn("Verify password bootstrap connection", playbook)
        self.assertIn("ECP_HOSTNAME=", playbook)
        self.assertGreaterEqual(playbook.count("no_log: true"), 2)

    def test_enrollment_uses_remote_hostname_when_inventory_name_is_omitted(self):
        headers = self.login()
        self.main.stage_controller_key(ed25519.Ed25519PrivateKey.generate(), "generated")
        with patch.object(self.main, "launch_key_enrollment_probe", return_value=89):
            response = self.client.post("/api/nodes/enroll", headers=headers, json={
                "name": "", "address": "192.0.2.101", "ssh_port": 22, "ssh_user": "root", "enabled": True,
                "auth_method": "controller_key", "install_controller_key": True,
            })
        self.assertEqual(response.status_code, 201)
        with self.main.db() as con:
            node = con.execute("SELECT name FROM nodes WHERE id=?", (response.json()["id"],)).fetchone()
            run_id = con.execute(
                "INSERT INTO runs(kind,target,status,command_json,log,context_json) VALUES ('host-enroll','pending','running','[]',?,?)",
                ("ECP_HOSTNAME=target-alpha\n", json.dumps({
                    "enrollment_node_id": response.json()["id"], "enrollment_enabled": True,
                    "enrollment_existing_key": True, "enrollment_auto_name": True, "enrollment_username": "operator",
                })),
            ).lastrowid
        self.assertTrue(node["name"].startswith("pending-"))
        asyncio.run(self.main.run(run_id, ["/usr/bin/true"]))
        with self.main.db() as con:
            node = con.execute("SELECT name FROM nodes WHERE id=?", (response.json()["id"],)).fetchone()
        self.assertEqual(node["name"], "target-alpha")

    def test_node_addresses_must_be_ip_literals(self):
        headers = self.login()
        rejected = self.client.post("/api/nodes", headers=headers, json={
            "name": "dns-host", "address": "host.example.test", "ssh_port": 22, "ssh_user": "root", "enabled": True,
        })
        self.assertEqual(rejected.status_code, 422)

    def test_host_key_replacement_is_audited_and_controller_key_deletion_requires_confirmation(self):
        headers = self.login()
        managed_key = self.main.stage_controller_key(ed25519.Ed25519PrivateKey.generate(), "generated")
        node_id = self.node(headers)
        host_key = ed25519.Ed25519PrivateKey.generate().public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()
        update = self.client.put(f"/api/nodes/{node_id}", headers=headers, json={
            "name": "node-a", "address": "192.0.2.102", "ssh_port": 22, "ssh_user": "root", "enabled": True,
            "ssh_host_key": host_key,
        })
        self.assertEqual(update.status_code, 200)
        with self.main.db() as con:
            node = con.execute("SELECT ssh_host_key FROM nodes WHERE id=?", (node_id,)).fetchone()
            audit = con.execute("SELECT action FROM audit_events ORDER BY id DESC LIMIT 1").fetchone()
            con.execute("UPDATE nodes SET ssh_auth_state='controller_key',ssh_key_id=? WHERE id=?", (managed_key["key_id"], node_id))
        self.assertEqual(node["ssh_host_key"], host_key)
        self.assertEqual(audit["action"], "host_ssh_host_key_replaced")
        blocked = self.client.delete(f"/api/nodes/{node_id}", headers=headers)
        self.assertEqual(blocked.status_code, 409)
        deleted = self.client.delete(f"/api/nodes/{node_id}?records_only=true", headers=headers)
        self.assertEqual(deleted.status_code, 204)
        with self.main.db() as con:
            audit = con.execute("SELECT action FROM audit_events ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(audit["action"], "host_records_only_deletion")

    def test_command_streamer_uses_blocking_popen_pipes(self):
        process = Mock()
        process.stdout = io.StringIO("first line\nsecond line\n")
        process.wait.return_value = 0
        lines = []
        with patch.object(self.main.subprocess, "Popen", return_value=process) as popen:
            status = self.main.stream_command(["ansible-playbook", "--version"], lines.append)
        self.assertEqual(status, 0)
        self.assertEqual(lines, ["first line\n", "second line\n"])
        self.assertEqual(popen.call_args.kwargs["stdin"], self.main.subprocess.DEVNULL)
        self.assertEqual(popen.call_args.kwargs["stdout"], self.main.subprocess.PIPE)
        self.assertEqual(popen.call_args.kwargs["stderr"], self.main.subprocess.STDOUT)
        self.assertTrue(popen.call_args.kwargs["text"])
        self.assertTrue(process.stdout.closed)

    def test_cluster_membership_assignment_and_resource_update(self):
        headers = self.login()
        node_id = self.node(headers)
        cluster_id = self.cluster(headers)
        result = self.membership(headers, cluster_id, node_id)
        self.assertEqual(result.status_code, 201)
        result = self.client.post(f"/api/clusters/{cluster_id}/assignments", headers=headers, json={
            "node_id": node_id,
            "role": "master",
            "config": {"cpu": "2", "memory": "4g", "storage_path": "/srv/elastic/lab-a/master"},
        })
        self.assertEqual(result.status_code, 201)
        assignment_id = result.json()["id"]
        result = self.client.put(f"/api/assignments/{assignment_id}/resources", headers=headers, json={
            "cpu": "3", "memory": "6g", "storage_path": "/srv/elastic/lab-a/master",
        })
        self.assertEqual(result.status_code, 200)
        assignment = self.client.get(f"/api/clusters/{cluster_id}", headers=headers).json()["assignments"][0]
        self.assertEqual(assignment["config"]["cpu"], "3")
        self.assertEqual(assignment["config"]["memory"], "6g")
        self.assertEqual(self.client.delete(f"/api/assignments/{assignment_id}?mode=detach", headers=headers).status_code, 200)

    def test_cluster_zoning_catalog_and_host_zone_selection(self):
        headers = self.login()
        cluster = self.client.post("/api/clusters", headers=headers, json={
            "name": "zoned-lab",
            "zoning": {"mode": "awareness", "zones": ["zone-a", "zone-b", "zone-c"]},
        })
        self.assertEqual(cluster.status_code, 201)
        cluster_id = cluster.json()["id"]
        node_id = self.node(headers)

        zone = self.client.put(f"/api/nodes/{node_id}/zone", headers=headers, json={
            "cluster_id": cluster_id,
            "zone_id": "zone-a",
        })
        self.assertEqual(zone.status_code, 200)
        self.assertIn("run_id", zone.json())
        self.assertEqual(self.client.get("/api/nodes", headers=headers).json()[0]["zone_id"], "zone-a")

        self.assertEqual(self.membership(headers, cluster_id, node_id).status_code, 201)
        record = self.client.get(f"/api/clusters/{cluster_id}", headers=headers).json()
        self.assertEqual(record["zoning"], {"mode": "awareness", "zones": ["zone-a", "zone-b", "zone-c"]})
        self.assertEqual(record["members"][0]["zone_id"], "zone-a")

        other = self.client.post("/api/clusters", headers=headers, json={
            "name": "other-zoned-lab",
            "zoning": {"mode": "awareness", "zones": ["zone-b", "zone-c"]},
        })
        self.assertEqual(other.status_code, 201)
        incompatible = self.membership(headers, other.json()["id"], node_id)
        self.assertEqual(incompatible.status_code, 422)
        self.assertIn("host zone", incompatible.json()["detail"].lower())

    def test_zoning_catalog_validation_and_in_use_zone_removal(self):
        headers = self.login()
        too_few = self.client.post("/api/clusters", headers=headers, json={
            "name": "single-zone",
            "zoning": {"mode": "awareness", "zones": ["zone-a"]},
        })
        self.assertEqual(too_few.status_code, 422)
        self.assertIn("at least two", too_few.text.lower())
        duplicate = self.client.post("/api/clusters", headers=headers, json={
            "name": "duplicate-zones",
            "zoning": {"mode": "awareness", "zones": ["zone-a", "ZONE-A"]},
        })
        self.assertEqual(duplicate.status_code, 422)

        cluster_id = self.client.post("/api/clusters", headers=headers, json={
            "name": "catalog-lab",
            "zoning": {"mode": "awareness", "zones": ["zone-a", "zone-b"]},
        }).json()["id"]
        node_id = self.node(headers)
        self.assertEqual(self.client.put(f"/api/nodes/{node_id}/zone", headers=headers, json={
            "cluster_id": cluster_id,
            "zone_id": "zone-a",
        }).status_code, 200)
        self.assertEqual(self.membership(headers, cluster_id, node_id).status_code, 201)

        removed = self.client.put(f"/api/clusters/{cluster_id}/zoning", headers=headers, json={
            "mode": "awareness",
            "zones": ["zone-b", "zone-c"],
        })
        self.assertEqual(removed.status_code, 409)
        self.assertIn("zone-a", removed.json()["detail"])

        disabled = self.client.put(f"/api/clusters/{cluster_id}/zoning", headers=headers, json={
            "mode": "disabled",
            "zones": [],
        })
        self.assertEqual(disabled.status_code, 200)

    def test_zoning_disabled_cluster_does_not_block_a_shared_host_zone_change(self):
        headers = self.login()
        zoned_cluster = self.client.post("/api/clusters", headers=headers, json={
            "name": "zone-owner",
            "zoning": {"mode": "awareness", "zones": ["zone-a", "zone-b"]},
        }).json()["id"]
        disabled_cluster = self.cluster(headers, "zone-disabled")
        node_id = self.node(headers, "shared-zone-node", "192.0.2.109")
        self.assertEqual(self.membership(headers, disabled_cluster, node_id).status_code, 201)

        result = self.client.put(f"/api/nodes/{node_id}/zone", headers=headers, json={
            "cluster_id": zoned_cluster,
            "zone_id": "zone-a",
        })

        self.assertEqual(result.status_code, 200)
        node = next(item for item in self.client.get("/api/nodes", headers=headers).json() if item["id"] == node_id)
        self.assertEqual(node["zone_id"], "zone-a")

    def test_awareness_blocks_elasticsearch_placement_without_a_defined_host_zone(self):
        headers = self.login()
        cluster_id = self.client.post("/api/clusters", headers=headers, json={
            "name": "placement-zones",
            "zoning": {"mode": "awareness", "zones": ["zone-a", "zone-b"]},
        }).json()["id"]
        node_id = self.node(headers)
        self.assertEqual(self.membership(headers, cluster_id, node_id).status_code, 422)

        with self.main.db() as con:
            con.execute(
                "INSERT INTO memberships(cluster_id,node_id,network_mode,data_interface,data_address,user_interface,user_address) VALUES (?,?,?,?,?,?,?)",
                (cluster_id, node_id, "dedicated", "ens19", "198.51.100.102", "ens18", "192.0.2.102"),
            )
        result = self.client.post(f"/api/clusters/{cluster_id}/workload-changes/apply", headers=headers, json={
            "changes": [{
                "client_id": "zoned-master", "kind": "create", "node_id": node_id, "role": "master",
                "config": {"cpu": "2", "memory": "4g", "storage_path": "/srv/zones/master"},
            }],
        })
        self.assertEqual(result.status_code, 422)
        self.assertIn("zone", result.json()["detail"].lower())

    def test_zoning_apply_rolls_data_before_master_and_records_observed_zones(self):
        headers = self.login()
        cluster_id = self.client.post("/api/clusters", headers=headers, json={
            "name": "apply-zones",
            "zoning": {"mode": "awareness", "zones": ["zone-a", "zone-b"]},
        }).json()["id"]
        first = self.node(headers, "zone-node-a", "192.0.2.111")
        second = self.node(headers, "zone-node-b", "192.0.2.112")
        for node_id, zone_id, user_address, data_address in (
            (first, "zone-a", "192.0.2.111", "198.51.100.111"),
            (second, "zone-b", "192.0.2.112", "198.51.100.112"),
        ):
            self.assertEqual(self.client.put(f"/api/nodes/{node_id}/zone", headers=headers, json={
                "cluster_id": cluster_id, "zone_id": zone_id,
            }).status_code, 200)
            self.assertEqual(self.membership(headers, cluster_id, node_id, user_address, data_address).status_code, 201)
        for node_id, role, path in (
            (first, "master", "/srv/zones/master"),
            (first, "hot", "/srv/zones/hot-a"),
            (second, "hot", "/srv/zones/hot-b"),
        ):
            self.assertEqual(self.client.post(f"/api/clusters/{cluster_id}/assignments", headers=headers, json={
                "node_id": node_id, "role": role,
                "config": {"cpu": "2", "memory": "4g", "storage_path": path},
            }).status_code, 201)

        def stop_task(coroutine):
            coroutine.close()
            return None

        with patch.object(self.main.asyncio, "create_task", side_effect=stop_task):
            response = self.client.post(f"/api/clusters/{cluster_id}/zoning/apply", headers=headers)
        self.assertEqual(response.status_code, 200)
        run_id = response.json()["run_id"]
        invoked = []

        async def reconcile(_run_id, _inventory, payload, _name, _suffix):
            invoked.append((payload["assignment"]["role"], payload["membership"]["zone_id"]))
            return True

        async def settings(*_args):
            return True

        with patch.object(self.main, "execute_zoning_reconcile", side_effect=reconcile), patch.object(self.main, "execute_zoning_settings", side_effect=settings):
            asyncio.run(self.main.run_zoning_apply(run_id, cluster_id, self.main.INVENTORIES / f"run-{run_id}.yaml"))

        self.assertEqual(invoked, [("hot", "zone-a"), ("hot", "zone-b"), ("master", "zone-a")])
        zoning = self.client.get(f"/api/clusters/{cluster_id}/zoning", headers=headers).json()
        self.assertEqual(zoning["status"]["applied_mode"], "awareness")
        self.assertEqual(set(zoning["status"]["observed_zones"].values()), {"zone-a", "zone-b"})
        with self.main.db() as con:
            self.assertEqual(con.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()["status"], "succeeded")

    def test_forced_awareness_apply_rejects_an_uncovered_zone(self):
        headers = self.login()
        cluster_id = self.client.post("/api/clusters", headers=headers, json={
            "name": "forced-zones",
            "zoning": {"mode": "forced_awareness", "zones": ["zone-a", "zone-b", "zone-c"]},
        }).json()["id"]
        for index, zone_id in enumerate(("zone-a", "zone-b"), start=1):
            node_id = self.node(headers, f"forced-{index}", f"192.0.2.{120 + index}")
            self.client.put(f"/api/nodes/{node_id}/zone", headers=headers, json={"cluster_id": cluster_id, "zone_id": zone_id})
            self.membership(headers, cluster_id, node_id, f"192.0.2.{120 + index}", f"198.51.100.{120 + index}")
            role = "master" if index == 1 else "hot"
            self.client.post(f"/api/clusters/{cluster_id}/assignments", headers=headers, json={
                "node_id": node_id, "role": role,
                "config": {"cpu": "2", "memory": "4g", "storage_path": f"/srv/forced/{role}"},
            })
        result = self.client.post(f"/api/clusters/{cluster_id}/zoning/apply", headers=headers)
        self.assertEqual(result.status_code, 422)
        self.assertIn("zone-c", result.json()["detail"])

    def test_disabling_zoning_reconciles_runtime_node_attributes_to_empty(self):
        headers = self.login()
        cluster_id = self.cluster(headers, "disable-zones")
        node_id = self.node(headers, "disable-zone-node", "192.0.2.121")
        self.client.post(f"/api/clusters/{cluster_id}/members", headers=headers, json={
            "node_id": node_id, "network_mode": "shared", "data_interface": "ens18", "data_address": "192.0.2.121",
            "user_interface": "ens18", "user_address": "192.0.2.121",
        })
        assignment_id = self.client.post(f"/api/clusters/{cluster_id}/assignments", headers=headers, json={
            "node_id": node_id, "role": "master",
            "config": {"cpu": "2", "memory": "4g", "storage_path": "/srv/disable-zones/master"},
        }).json()["id"]
        with self.main.db() as con:
            con.execute("UPDATE nodes SET zone_id='zone-a' WHERE id=?", (node_id,))
            con.execute(
                "INSERT INTO cluster_zoning_observations(cluster_id,applied_mode,applied_zones_json,observed_zones_json,status) VALUES (?,'awareness',?,?,'applied')",
                (cluster_id, json.dumps(["zone-a", "zone-b"]), json.dumps({str(assignment_id): "zone-a"})),
            )
            run_id = con.execute(
                "INSERT INTO runs(kind,target,status,command_json,context_json) VALUES ('zoning-apply','disable-zones:zoning','running','[]','{}')"
            ).lastrowid
        observed = []

        async def reconcile(_run_id, _inventory, payload, _name, _suffix):
            observed.append(payload["membership"]["zone_id"])
            return True

        with patch.object(self.main, "execute_zoning_reconcile", side_effect=reconcile), patch.object(self.main, "execute_zoning_settings", return_value=True):
            asyncio.run(self.main.run_zoning_apply(run_id, cluster_id, self.main.INVENTORIES / f"run-{run_id}.yaml"))
        self.assertEqual(observed, [""])
        status = self.client.get(f"/api/clusters/{cluster_id}/zoning", headers=headers).json()["status"]
        self.assertEqual(status["applied_mode"], "disabled")
        self.assertEqual(status["observed_zones"], {str(assignment_id): ""})

    def test_active_host_zone_change_reconciles_and_restores_the_previous_zone_on_failure(self):
        headers = self.login()
        cluster_id = self.client.post("/api/clusters", headers=headers, json={
            "name": "move-zone",
            "zoning": {"mode": "awareness", "zones": ["zone-a", "zone-b", "zone-c"]},
        }).json()["id"]
        first = self.node(headers, "move-master", "192.0.2.131")
        second = self.node(headers, "move-hot", "192.0.2.132")
        for node_id, zone_id, user_address, data_address in (
            (first, "zone-a", "192.0.2.131", "198.51.100.131"),
            (second, "zone-b", "192.0.2.132", "198.51.100.132"),
        ):
            self.client.put(f"/api/nodes/{node_id}/zone", headers=headers, json={"cluster_id": cluster_id, "zone_id": zone_id})
            self.membership(headers, cluster_id, node_id, user_address, data_address)
        self.client.post(f"/api/clusters/{cluster_id}/assignments", headers=headers, json={
            "node_id": first, "role": "master",
            "config": {"cpu": "2", "memory": "4g", "storage_path": "/srv/move/master"},
        })
        self.client.post(f"/api/clusters/{cluster_id}/assignments", headers=headers, json={
            "node_id": second, "role": "hot",
            "config": {"cpu": "2", "memory": "4g", "storage_path": "/srv/move/hot"},
        })

        def stop_task(coroutine):
            coroutine.close()
            return None

        with patch.object(self.main.asyncio, "create_task", side_effect=stop_task):
            response = self.client.put(f"/api/nodes/{first}/zone", headers=headers, json={
                "cluster_id": cluster_id, "zone_id": "zone-c",
            })
        self.assertEqual(response.status_code, 200)
        run_id = response.json()["run_id"]
        self.assertEqual(next(node for node in self.client.get("/api/nodes", headers=headers).json() if node["id"] == first)["zone_id"], "zone-c")
        with self.main.db() as con:
            assignment = con.execute(
                "SELECT operation_run_id FROM cluster_assignments WHERE node_id=? AND role='master'",
                (first,),
            ).fetchone()
            self.assertEqual(assignment["operation_run_id"], run_id)
            self.assertIsNotNone(self.main.active_cluster_operation(con, "move-zone"))
        invoked = []

        async def reconcile(_run_id, _inventory, payload, _name, _suffix):
            invoked.append(payload["membership"]["zone_id"])
            return False

        with patch.object(self.main, "execute_zoning_reconcile", side_effect=reconcile):
            asyncio.run(self.main.run_host_zone_change(run_id, first, "zone-a", "zone-c", self.main.INVENTORIES / f"run-{run_id}.yaml"))
        self.assertEqual(invoked, ["zone-c"])
        self.assertEqual(next(node for node in self.client.get("/api/nodes", headers=headers).json() if node["id"] == first)["zone_id"], "zone-a")
        with self.main.db() as con:
            self.assertEqual(con.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()["status"], "failed")
            self.assertIsNone(con.execute(
                "SELECT operation_run_id FROM cluster_assignments WHERE node_id=? AND role='master'",
                (first,),
            ).fetchone()["operation_run_id"])

    def test_workload_batch_keeps_new_roles_out_of_managed_workloads_until_it_succeeds(self):
        headers = self.login()
        node_id = self.node(headers)
        cluster_id = self.cluster(headers)
        self.assertEqual(self.membership(headers, cluster_id, node_id).status_code, 201)

        def stop_task(coroutine):
            coroutine.close()
            return None

        with patch.object(self.main.asyncio, "create_task", side_effect=stop_task):
            result = self.client.post(f"/api/clusters/{cluster_id}/workload-changes/apply", headers=headers, json={
                "changes": [{
                    "client_id": "new-master", "kind": "create", "node_id": node_id, "role": "master",
                    "image_version": "8.20.0",
                    "config": {"cpu": "2", "memory": "4g", "storage_path": "/srv/batch/master"},
                }],
            })
        self.assertEqual(result.status_code, 200)
        run_id = result.json()["run_id"]
        self.assertEqual(self.client.get(f"/api/clusters/{cluster_id}", headers=headers).json()["assignments"], [])
        with self.main.db() as con:
            applying = con.execute("SELECT state,operation_run_id,image_version FROM cluster_assignments").fetchone()
            plan = con.execute("SELECT plan_encrypted FROM workload_change_batches WHERE run_id=?", (run_id,)).fetchone()["plan_encrypted"]
        self.assertEqual((applying["state"], applying["operation_run_id"]), ("applying", run_id))
        self.assertEqual(applying["image_version"], "8.20.0")
        self.assertNotIn("/srv/batch/master", plan)

    def test_workload_batch_rejects_an_invalid_image_version(self):
        headers = self.login()
        node_id = self.node(headers)
        cluster_id = self.cluster(headers)
        self.assertEqual(self.membership(headers, cluster_id, node_id).status_code, 201)
        result = self.client.post(f"/api/clusters/{cluster_id}/workload-changes/apply", headers=headers, json={
            "changes": [{
                "client_id": "new-master", "kind": "create", "node_id": node_id, "role": "master",
                "image_version": "latest",
                "config": {"cpu": "2", "memory": "4g", "storage_path": "/srv/batch/master"},
            }],
        })
        self.assertEqual(result.status_code, 422)

    def test_workload_batch_promotes_all_roles_only_after_success(self):
        headers = self.login()
        node_id = self.node(headers)
        cluster_id = self.cluster(headers)
        self.assertEqual(self.membership(headers, cluster_id, node_id).status_code, 201)

        def stop_task(coroutine):
            coroutine.close()
            return None

        with patch.object(self.main.asyncio, "create_task", side_effect=stop_task):
            result = self.client.post(f"/api/clusters/{cluster_id}/workload-changes/apply", headers=headers, json={
                "changes": [
                    {"client_id": "new-kibana", "kind": "create", "node_id": node_id, "role": "kibana", "config": {"cpu": "1", "memory": "2g", "storage_path": "/srv/batch/kibana"}},
                    {"client_id": "new-master", "kind": "create", "node_id": node_id, "role": "master", "config": {"cpu": "2", "memory": "4g", "storage_path": "/srv/batch/master"}},
                ],
            })
        run_id = result.json()["run_id"]
        invoked = []

        async def reconcile(_run_id, _inventory, payload, _name, _suffix):
            invoked.append(payload["assignment"]["role"])
            return True

        with patch.object(self.main, "execute_workload_change_reconcile", side_effect=reconcile):
            asyncio.run(self.main.run_workload_change_batch(run_id, self.main.INVENTORIES / f"run-{run_id}.yaml"))
        self.assertEqual(invoked, ["master", "kibana"])
        assigned = self.client.get(f"/api/clusters/{cluster_id}", headers=headers).json()["assignments"]
        self.assertEqual([item["role"] for item in assigned], ["kibana", "master"])
        self.assertTrue(all(item["state"] == "active" for item in assigned))

    def test_workload_batch_rolls_back_resource_edits_and_new_roles(self):
        headers = self.login()
        node_id = self.node(headers)
        cluster_id = self.cluster(headers)
        self.assertEqual(self.membership(headers, cluster_id, node_id).status_code, 201)
        master_id = self.client.post(f"/api/clusters/{cluster_id}/assignments", headers=headers, json={
            "node_id": node_id, "role": "master", "config": {"cpu": "2", "memory": "4g", "storage_path": "/srv/batch/master"},
        }).json()["id"]

        def stop_task(coroutine):
            coroutine.close()
            return None

        with patch.object(self.main.asyncio, "create_task", side_effect=stop_task):
            result = self.client.post(f"/api/clusters/{cluster_id}/workload-changes/apply", headers=headers, json={
                "changes": [
                    {"client_id": "master-resources", "kind": "resources", "assignment_id": master_id, "expected_revision": 1, "config": {"cpu": "3", "memory": "6g", "storage_path": "/srv/batch/master"}},
                    {"client_id": "new-kibana", "kind": "create", "node_id": node_id, "role": "kibana", "config": {"cpu": "1", "memory": "2g", "storage_path": "/srv/batch/kibana"}},
                ],
            })
        run_id = result.json()["run_id"]
        outcomes = iter((True, False, True, True))

        async def reconcile(*_args):
            return next(outcomes)

        with patch.object(self.main, "execute_workload_change_reconcile", side_effect=reconcile):
            asyncio.run(self.main.run_workload_change_batch(run_id, self.main.INVENTORIES / f"run-{run_id}.yaml"))
        assigned = self.client.get(f"/api/clusters/{cluster_id}", headers=headers).json()["assignments"]
        self.assertEqual(len(assigned), 1)
        self.assertEqual(assigned[0]["config"]["cpu"], "2")
        with self.main.db() as con:
            run = con.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
            self.assertIsNone(con.execute("SELECT 1 FROM workload_change_batches WHERE run_id=?", (run_id,)).fetchone())
        self.assertEqual(run["status"], "failed")

    def test_workload_batch_rejects_a_stale_managed_workload_revision(self):
        headers = self.login()
        node_id = self.node(headers)
        cluster_id = self.cluster(headers)
        self.assertEqual(self.membership(headers, cluster_id, node_id).status_code, 201)
        assignment_id = self.client.post(f"/api/clusters/{cluster_id}/assignments", headers=headers, json={
            "node_id": node_id, "role": "master", "config": {"cpu": "2", "memory": "4g", "storage_path": "/srv/batch/master"},
        }).json()["id"]
        result = self.client.post(f"/api/clusters/{cluster_id}/workload-changes/apply", headers=headers, json={
            "changes": [{
                "client_id": "stale-master", "kind": "resources", "assignment_id": assignment_id, "expected_revision": 9,
                "config": {"cpu": "3", "memory": "6g", "storage_path": "/srv/batch/master"},
            }],
        })
        self.assertEqual(result.status_code, 409)
        self.assertIn("changed since it was staged", result.json()["detail"])

    def test_workload_batch_defers_detach_until_the_batch_is_committed(self):
        headers = self.login()
        node_id = self.node(headers)
        cluster_id = self.cluster(headers)
        self.assertEqual(self.membership(headers, cluster_id, node_id).status_code, 201)
        assignment_id = self.client.post(f"/api/clusters/{cluster_id}/assignments", headers=headers, json={
            "node_id": node_id, "role": "master", "config": {"cpu": "2", "memory": "4g", "storage_path": "/srv/batch/master"},
        }).json()["id"]

        def stop_task(coroutine):
            coroutine.close()
            return None

        with patch.object(self.main.asyncio, "create_task", side_effect=stop_task):
            result = self.client.post(f"/api/clusters/{cluster_id}/workload-changes/apply", headers=headers, json={
                "changes": [{"client_id": "detach-master", "kind": "detach", "assignment_id": assignment_id, "expected_revision": 1}],
            })
        run_id = result.json()["run_id"]
        with self.main.db() as con:
            locked = con.execute("SELECT operation_run_id FROM cluster_assignments WHERE id=?", (assignment_id,)).fetchone()
        self.assertEqual(locked["operation_run_id"], run_id)
        asyncio.run(self.main.run_workload_change_batch(run_id, self.main.INVENTORIES / f"run-{run_id}.yaml"))
        self.assertEqual(self.client.get(f"/api/clusters/{cluster_id}", headers=headers).json()["assignments"], [])

    def test_rejects_port_collision_for_two_clusters_on_one_host(self):
        headers = self.login()
        node_id = self.node(headers)
        first = self.cluster(headers, "lab-a")
        second = self.cluster(headers, "lab-b")
        for cluster_id in (first, second):
            self.assertEqual(self.membership(headers, cluster_id, node_id).status_code, 201)
        self.assertEqual(self.client.post(f"/api/clusters/{first}/assignments", headers=headers, json={
            "node_id": node_id, "role": "master",
            "config": {"cpu": "2", "memory": "4g", "storage_path": "/srv/elastic/lab-a/master"},
        }).status_code, 201)
        role_ports = self.client.get(f"/api/clusters/{second}", headers=headers).json()["role_ports"]
        role_ports["master"] = {"elasticsearch_http": 9210, "elasticsearch_transport": 9310}
        role_ports["hot"] = {"elasticsearch_http": 9200, "elasticsearch_transport": 9300}
        self.assertEqual(self.client.put(f"/api/clusters/{second}", headers=headers, json={"name": "lab-b", "role_ports": role_ports}).status_code, 200)
        self.assertEqual(self.client.post(f"/api/clusters/{second}/assignments", headers=headers, json={
            "node_id": node_id, "role": "hot",
            "config": {"cpu": "2", "memory": "4g", "storage_path": "/srv/elastic/lab-b/hot"},
        }).status_code, 409)

    def test_role_port_associations_suggest_distinct_ports_for_colocated_roles(self):
        headers = self.login()
        node_id = self.node(headers)
        cluster_id = self.cluster(headers)
        self.assertEqual(self.membership(headers, cluster_id, node_id).status_code, 201)
        role_ports = self.client.get(f"/api/clusters/{cluster_id}", headers=headers).json()["role_ports"]
        self.assertEqual(role_ports["master"], {"elasticsearch_http": 9200, "elasticsearch_transport": 9300})
        self.assertEqual(role_ports["hot"], {"elasticsearch_http": 9201, "elasticsearch_transport": 9301})

        def stop_task(coroutine):
            coroutine.close()
            return None

        with patch.object(self.main.asyncio, "create_task", side_effect=stop_task):
            result = self.client.post(f"/api/clusters/{cluster_id}/workload-changes/apply", headers=headers, json={
                "changes": [
                    {"client_id": "master", "kind": "create", "node_id": node_id, "role": "master", "config": {"cpu": "2", "memory": "4g", "storage_path": "/srv/ports/master"}},
                    {"client_id": "hot", "kind": "create", "node_id": node_id, "role": "hot", "config": {"cpu": "2", "memory": "4g", "storage_path": "/srv/ports/hot"}},
                ],
            })
        self.assertEqual(result.status_code, 200)

    def test_legacy_port_profile_derives_collision_free_role_ports(self):
        headers = self.login()
        cluster_id = self.cluster(headers, ports={
            "elasticsearch_http": 9200,
            "elasticsearch_transport": 9300,
            "kibana": 9201,
            "fleet": 8220,
            "logstash_api": 9600,
        })
        role_ports = self.client.get(f"/api/clusters/{cluster_id}", headers=headers).json()["role_ports"]
        self.assertEqual(role_ports["master"], {"elasticsearch_http": 9200, "elasticsearch_transport": 9300})
        self.assertEqual(role_ports["kibana"], {"kibana": 9201})
        self.assertNotIn(9201, role_ports["hot"].values())

    def test_rejects_duplicate_role_port_associations(self):
        headers = self.login()
        result = self.client.post("/api/clusters", headers=headers, json={
            "name": "port-collision",
            "role_ports": {
                "master": {"elasticsearch_http": 9200, "elasticsearch_transport": 9300},
                "hot": {"elasticsearch_http": 9200, "elasticsearch_transport": 9301},
                "warm": {"elasticsearch_http": 9202, "elasticsearch_transport": 9302},
                "ml": {"elasticsearch_http": 9203, "elasticsearch_transport": 9303},
                "ingest": {"elasticsearch_http": 9204, "elasticsearch_transport": 9304},
                "coordinating": {"elasticsearch_http": 9205, "elasticsearch_transport": 9305},
                "kibana": {"kibana": 5601},
                "fleet-server": {"fleet": 8220},
                "logstash": {"logstash_api": 9600},
                "elastic-agent": {},
            },
        })
        self.assertEqual(result.status_code, 422)

    def test_purge_returns_a_run_id_for_the_ui_to_watch(self):
        headers = self.login()
        node_id = self.node(headers)
        cluster_id = self.cluster(headers)
        self.assertEqual(self.membership(headers, cluster_id, node_id).status_code, 201)
        assignment_id = self.client.post(f"/api/clusters/{cluster_id}/assignments", headers=headers, json={
            "node_id": node_id, "role": "master",
            "config": {"cpu": "2", "memory": "4g", "storage_path": "/srv/purge/master"},
        }).json()["id"]
        with patch.object(self.main, "launch", return_value=77):
            result = self.client.delete(f"/api/assignments/{assignment_id}?mode=purge", headers=headers)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json(), {"run_id": 77})

    def test_rejects_invalid_role_and_storage_path(self):
        headers = self.login()
        node_id = self.node(headers)
        cluster_id = self.cluster(headers)
        self.membership(headers, cluster_id, node_id)
        self.assertEqual(self.client.post(f"/api/clusters/{cluster_id}/assignments", headers=headers, json={
            "node_id": node_id, "role": "unknown",
            "config": {"cpu": "2", "memory": "4g", "storage_path": "/srv/x"},
        }).status_code, 422)
        self.assertEqual(self.client.post(f"/api/clusters/{cluster_id}/assignments", headers=headers, json={
            "node_id": node_id, "role": "master",
            "config": {"cpu": "2", "memory": "4g", "storage_path": "/etc/elastic"},
        }).status_code, 422)

    def test_rejects_malformed_token(self):
        self.assertEqual(self.client.get("/api/nodes", headers={"Authorization": "Bearer %%not-base64%%"}).status_code, 401)

    def test_allows_multiple_masters_and_builds_join_payload(self):
        headers = self.login()
        first_node = self.node(headers, "node-a", "192.0.2.102")
        second_node = self.node(headers, "node-b", "192.0.2.103")
        cluster_id = self.cluster(headers)
        for node_id, address, data_address in ((first_node, "192.0.2.102", "198.51.100.102"), (second_node, "192.0.2.103", "198.51.100.103")):
            self.assertEqual(self.membership(headers, cluster_id, node_id, address, data_address).status_code, 201)
        first = self.client.post(f"/api/clusters/{cluster_id}/assignments", headers=headers, json={"node_id": first_node, "role": "master", "config": {"cpu": "2", "memory": "4g", "storage_path": "/srv/elastic/a"}})
        second = self.client.post(f"/api/clusters/{cluster_id}/assignments", headers=headers, json={"node_id": second_node, "role": "master", "config": {"cpu": "2", "memory": "4g", "storage_path": "/srv/elastic/b"}})
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        with self.main.db() as con:
            payload = self.main.cluster_payload(con, self.main.assignment_record(con, second.json()["id"]))
        self.assertEqual(payload["bootstrap"]["assignment_id"], first.json()["id"])
        self.assertEqual(
            [(item["node_id"], item["data_address"]) for item in payload["masters"]],
            [(first_node, "198.51.100.102"), (second_node, "198.51.100.103")],
        )
        self.assertEqual(self.client.post(f"/api/clusters/{cluster_id}/assignments", headers=headers, json={"node_id": second_node, "role": "hot", "config": {"cpu": "2", "memory": "1g", "storage_path": "/srv/elastic/hot"}}).status_code, 422)

    def test_preserves_the_initial_master_while_other_workloads_exist(self):
        headers = self.login()
        first_node = self.node(headers, "node-a", "192.0.2.102")
        second_node = self.node(headers, "node-b", "192.0.2.103")
        cluster_id = self.cluster(headers)
        for node_id, address, data_address in ((first_node, "192.0.2.102", "198.51.100.102"), (second_node, "192.0.2.103", "198.51.100.103")):
            self.assertEqual(self.membership(headers, cluster_id, node_id, address, data_address).status_code, 201)
        first = self.client.post(f"/api/clusters/{cluster_id}/assignments", headers=headers, json={"node_id": first_node, "role": "master", "config": {"cpu": "2", "memory": "4g", "storage_path": "/srv/elastic/a"}}).json()["id"]
        second = self.client.post(f"/api/clusters/{cluster_id}/assignments", headers=headers, json={"node_id": second_node, "role": "master", "config": {"cpu": "2", "memory": "4g", "storage_path": "/srv/elastic/b"}}).json()["id"]
        self.assertEqual(self.client.delete(f"/api/assignments/{first}?mode=detach", headers=headers).status_code, 409)
        self.assertEqual(self.client.delete(f"/api/assignments/{second}?mode=detach", headers=headers).status_code, 200)

    def test_service_apply_requires_its_coordinators(self):
        headers = self.login()
        node_id = self.node(headers)
        cluster_id = self.cluster(headers)
        self.membership(headers, cluster_id, node_id)
        result = self.client.post(f"/api/clusters/{cluster_id}/assignments", headers=headers, json={"node_id": node_id, "role": "fleet-server", "config": {"cpu": "1", "memory": "2g", "storage_path": "/srv/elastic/fleet"}})
        assignment_id = result.json()["id"]
        self.assertEqual(self.client.post(f"/api/assignments/{assignment_id}/apply", headers=headers).status_code, 422)
        with self.main.db() as con:
            row = self.main.assignment_record(con, assignment_id)
            self.assertEqual(self.main.cluster_payload(con, row, "purge")["desired_state"], "purge")

    def test_topology_redacts_secret_advanced_config(self):
        headers = self.login()
        node_id = self.node(headers)
        cluster_id = self.cluster(headers)
        self.membership(headers, cluster_id, node_id)
        self.client.post(f"/api/clusters/{cluster_id}/assignments", headers=headers, json={"node_id": node_id, "role": "master", "config": {"cpu": "2", "memory": "4g", "storage_path": "/srv/elastic/master", "api_key": "not-visible"}})
        record = self.client.get(f"/api/clusters/{cluster_id}", headers=headers).json()
        self.assertEqual(record["assignments"][0]["config"]["api_key"], "configured")
        diagram = self.client.get(f"/api/clusters/{cluster_id}/topology", headers=headers).json()["topology"]
        self.assertIn("HOST: node-a", diagram)
        self.assertIn("Network : dedicated", diagram)
        self.assertIn("User NIC: ens18  192.0.2.102", diagram)
        self.assertIn("Data NIC: ens19  198.51.100.102", diagram)

    def test_membership_rejects_legacy_or_non_distinct_bindings_and_supports_update(self):
        headers = self.login()
        node_id = self.node(headers)
        cluster_id = self.cluster(headers)
        legacy = self.client.post(f"/api/clusters/{cluster_id}/members", headers=headers, json={"node_id": node_id, "advertised_address": "192.0.2.102"})
        self.assertEqual(legacy.status_code, 422)
        self.assertIn("advertised_address has been replaced", str(legacy.json()))
        invalid = self.client.post(f"/api/clusters/{cluster_id}/members", headers=headers, json={
            "node_id": node_id, "data_interface": "ens18", "data_address": "198.51.100.102",
            "user_interface": "ens18", "user_address": "192.0.2.102",
        })
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(self.membership(headers, cluster_id, node_id).status_code, 201)
        record = self.client.get(f"/api/clusters/{cluster_id}", headers=headers).json()["members"][0]
        self.assertTrue(record["network_ready"])
        mismatch = self.client.put(f"/api/clusters/{cluster_id}/members/{node_id}", headers=headers, json={
            "node_id": node_id + 1, "data_interface": "ens19", "data_address": "198.51.100.104",
            "user_interface": "ens18", "user_address": "192.0.2.104",
        })
        self.assertEqual(mismatch.status_code, 422)
        updated = self.client.put(f"/api/clusters/{cluster_id}/members/{node_id}", headers=headers, json={
            "node_id": node_id, "data_interface": "ens20", "data_address": "10.20.0.102",
            "user_interface": "ens18", "user_address": "192.0.2.102",
        })
        self.assertEqual(updated.status_code, 200)
        member = self.client.get(f"/api/clusters/{cluster_id}", headers=headers).json()["members"][0]
        self.assertEqual((member["data_interface"], member["data_address"]), ("ens20", "10.20.0.102"))

    def test_shared_network_membership_is_explicit_and_ready(self):
        headers = self.login()
        node_id = self.node(headers)
        cluster_id = self.cluster(headers)
        result = self.client.post(f"/api/clusters/{cluster_id}/members", headers=headers, json={
            "node_id": node_id,
            "network_mode": "shared",
            "data_interface": "ens18",
            "data_address": "192.0.2.102",
            "user_interface": "ens18",
            "user_address": "192.0.2.102",
        })
        self.assertEqual(result.status_code, 201)
        member = self.client.get(f"/api/clusters/{cluster_id}", headers=headers).json()["members"][0]
        self.assertEqual(member["network_mode"], "shared")
        self.assertTrue(member["network_ready"])

    def test_shared_network_service_assignment_does_not_block_master_payload(self):
        headers = self.login()
        node_id = self.node(headers)
        cluster_id = self.cluster(headers)
        self.assertEqual(self.client.post(f"/api/clusters/{cluster_id}/members", headers=headers, json={
            "node_id": node_id, "network_mode": "shared",
            "data_interface": "ens18", "data_address": "192.0.2.102",
            "user_interface": "ens18", "user_address": "192.0.2.102",
        }).status_code, 201)
        master = self.client.post(f"/api/clusters/{cluster_id}/assignments", headers=headers, json={
            "node_id": node_id, "role": "master", "config": {"cpu": "2", "memory": "2g", "storage_path": "/srv/shared/master"},
        }).json()["id"]
        self.assertEqual(self.client.post(f"/api/clusters/{cluster_id}/assignments", headers=headers, json={
            "node_id": node_id, "role": "kibana", "config": {"cpu": "1", "memory": "2g", "storage_path": "/srv/shared/kibana"},
        }).status_code, 201)
        with self.main.db() as con:
            payload = self.main.cluster_payload(con, self.main.assignment_record(con, master))
        self.assertEqual(payload["membership"]["network_mode"], "shared")

    def test_membership_insert_supports_legacy_required_advertised_address(self):
        headers = self.login()
        node_id = self.node(headers)
        cluster_id = self.cluster(headers)
        with self.main.db() as con:
            con.execute("DROP TABLE memberships")
            con.execute("""
                CREATE TABLE memberships (
                  cluster_id INTEGER NOT NULL, node_id INTEGER NOT NULL,
                  advertised_address TEXT NOT NULL, network_mode TEXT NOT NULL DEFAULT 'dedicated',
                  data_interface TEXT, data_address TEXT, user_interface TEXT, user_address TEXT,
                  PRIMARY KEY(cluster_id, node_id)
                )
            """)
        result = self.client.post(f"/api/clusters/{cluster_id}/members", headers=headers, json={
            "node_id": node_id,
            "network_mode": "shared",
            "data_interface": "ens18",
            "data_address": "192.0.2.102",
            "user_interface": "ens18",
            "user_address": "192.0.2.102",
        })
        self.assertEqual(result.status_code, 201)
        with self.main.db() as con:
            row = con.execute("SELECT advertised_address,network_mode FROM memberships").fetchone()
        self.assertEqual((row["advertised_address"], row["network_mode"]), ("192.0.2.102", "shared"))

    def test_migrated_single_address_membership_blocks_apply_until_network_is_completed(self):
        headers = self.login()
        node_id = self.node(headers)
        cluster_id = self.cluster(headers)
        with self.main.db() as con:
            columns = {row["name"] for row in con.execute("PRAGMA table_info(memberships)")}
            if "advertised_address" not in columns:
                con.execute("ALTER TABLE memberships ADD COLUMN advertised_address TEXT")
            con.execute("INSERT INTO memberships(cluster_id,node_id,advertised_address) VALUES (?,?,?)", (cluster_id, node_id, "192.0.2.102"))
        self.main.init()
        member = self.client.get(f"/api/clusters/{cluster_id}", headers=headers).json()["members"][0]
        self.assertEqual(member["user_address"], "192.0.2.102")
        self.assertFalse(member["network_ready"])
        assignment = self.client.post(f"/api/clusters/{cluster_id}/assignments", headers=headers, json={
            "node_id": node_id, "role": "master", "config": {"cpu": "2", "memory": "4g", "storage_path": "/srv/migration/master"},
        })
        self.assertEqual(assignment.status_code, 201)
        apply = self.client.post(f"/api/assignments/{assignment.json()['id']}/apply", headers=headers)
        self.assertEqual(apply.status_code, 422)
        self.assertIn("dedicated or shared", apply.json()["detail"])
        with self.main.db() as con:
            row = self.main.assignment_record(con, assignment.json()["id"])
            payload = self.main.cluster_payload(con, row, "purge")
        self.assertEqual(payload["desired_state"], "purge")
        topology = self.client.get(f"/api/clusters/{cluster_id}/topology", headers=headers).json()["topology"]
        self.assertNotIn("None -> None", topology)

    def test_topology_access_urls_follow_role_ports_and_hide_agents(self):
        headers = self.login()
        first = self.node(headers, "node-a", "192.0.2.102")
        second = self.node(headers, "node-b", "192.0.2.103")
        ports = {"elasticsearch_http": 19200, "elasticsearch_transport": 19300, "kibana": 15601, "fleet": 18220, "logstash_api": 19600}
        cluster_id = self.cluster(headers, "access-lab", ports)
        for node_id, address, data_address in ((first, "192.0.2.102", "198.51.100.102"), (second, "192.0.2.103", "198.51.100.103")):
            self.assertEqual(self.membership(headers, cluster_id, node_id, address, data_address).status_code, 201)
        assignments = [
            (first, "master", {"cpu": "2", "memory": "4g", "storage_path": "/srv/access/master"}),
            (second, "hot", {"cpu": "2", "memory": "4g", "storage_path": "/srv/access/hot"}),
            (second, "kibana", {"cpu": "1", "memory": "2g", "storage_path": "/srv/access/kibana"}),
            (second, "fleet-server", {"cpu": "1", "memory": "2g", "storage_path": "/srv/access/fleet"}),
            (second, "logstash", {"cpu": "1", "memory": "2g", "storage_path": "/srv/access/logstash", "pipeline": "input { stdin {} } output { stdout {} }"}),
            (second, "elastic-agent", {"cpu": "1", "memory": "2g", "storage_path": "/srv/access/agent"}),
        ]
        for node_id, role, config in assignments:
            self.assertEqual(self.client.post(f"/api/clusters/{cluster_id}/assignments", headers=headers, json={"node_id": node_id, "role": role, "config": config}).status_code, 201)
        topology = self.client.get(f"/api/clusters/{cluster_id}/topology", headers=headers).json()
        self.assertEqual([entry["url"] for entry in topology["access_urls"]], [
            "https://192.0.2.103:15601", "https://192.0.2.102:19200",
            "https://192.0.2.103:19201", "https://192.0.2.103:18220", "http://192.0.2.103:19600",
        ])
        diagram = topology["topology"]
        self.assertIn("Configured user access:", diagram)
        self.assertIn("|  | Master", diagram)
        self.assertIn("Name     : ecp-access-lab-master-", diagram)
        self.assertIn("Roles    : master, remote_cluster_client", diagram)
        self.assertIn("HTTP     : https://192.0.2.102:19200", diagram)
        self.assertIn("Transport: 198.51.100.102:19300/tcp (TLS)", diagram)
        self.assertIn("|  | Kibana", diagram)
        self.assertIn("URL      : https://192.0.2.103:15601", diagram)
        self.assertIn("Connection: outbound TLS", diagram)
        self.assertIn("Elasticsearch transport", diagram)
        self.assertIn("198.51.100.102 -> 198.51.100.103:19301/tcp (TLS)", diagram)
        self.assertTrue(all(len(line) <= 80 for line in diagram.splitlines()))

    def test_cluster_and_topology_surface_workload_maintenance_progress(self):
        from app.modules.maintenance.workload_contracts import (
            DisruptionBudget,
            WorkloadMaintenancePlanInput,
            WorkloadMaintenanceTarget,
            WorkloadOperation,
            WorkloadRole,
        )
        from app.modules.maintenance.workload_engine import WorkloadMaintenancePlanService

        headers = self.login()
        node_id = self.node(headers)
        cluster_id = self.cluster(headers)
        self.assertEqual(self.membership(headers, cluster_id, node_id).status_code, 201)
        assignment = self.client.post(
            f"/api/clusters/{cluster_id}/assignments",
            headers=headers,
            json={"node_id": node_id, "role": "kibana", "config": {"cpu": "1", "memory": "2g", "storage_path": "/srv/progress/kibana"}},
        ).json()
        with self.main.db() as connection:
            WorkloadMaintenancePlanService(self.main.MaintenanceStore(connection)).create_preview(
                WorkloadMaintenancePlanInput(
                    target=WorkloadMaintenanceTarget(
                        assignment_id=assignment["id"],
                        cluster_id=cluster_id,
                        node_id=node_id,
                        role=WorkloadRole.KIBANA,
                        operation=WorkloadOperation.RESTART,
                        expected_name="ecp-progress-kibana",
                        expected_image="docker.elastic.co/kibana/kibana:8.19.1",
                        budget=DisruptionBudget(available_before=2, minimum_ready=1),
                    ),
                    reason="Inspect planned workload maintenance",
                    idempotency_key="api-workload-progress",
                ),
                requested_by="operator",
            )

        cluster = self.client.get(f"/api/clusters/{cluster_id}", headers=headers).json()
        progress = cluster["assignments"][0]["maintenance"]
        self.assertEqual(progress["lifecycle_state"], "blocked")
        self.assertFalse(progress["execution_enabled"])
        diagram = self.client.get(f"/api/clusters/{cluster_id}/topology", headers=headers).json()["topology"]
        self.assertIn("Maintenance: blocked", diagram)

    def test_cluster_monitoring_credential_is_encrypted_and_metricbeat_versions_are_required(self):
        headers = self.login()
        cluster_id = self.cluster(headers)
        with self.main.db() as con:
            encrypted = con.execute("SELECT secrets_json FROM clusters WHERE id=?", (cluster_id,)).fetchone()["secrets_json"]
        credentials = self.main.open_config(encrypted)
        self.assertTrue(credentials["monitoring_password"])
        self.assertNotIn(credentials["monitoring_password"], encrypted)
        repositories = self.main.cluster_repositories([{"role": "master"}, {"role": "fleet-server"}])
        self.assertIn("elasticsearch/elasticsearch", repositories)
        self.assertIn("beats/metricbeat", repositories)
        self.assertEqual(self.main.metricbeat_image("8.19.0"), "docker.elastic.co/beats/metricbeat:8.19.0")
        with self.main.db() as con:
            con.execute(
                "UPDATE clusters SET secrets_json=? WHERE id=?",
                (self.main.seal_config(json.dumps({"elastic_password": "elastic", "kibana_password": "kibana"})), cluster_id),
            )
        self.main.init()
        with self.main.db() as con:
            migrated = con.execute("SELECT secrets_json FROM clusters WHERE id=?", (cluster_id,)).fetchone()["secrets_json"]
        self.assertTrue(self.main.open_config(migrated)["monitoring_password"])

    def test_kibana_reconcile_uses_readable_config_and_verifies_service_credential(self):
        playbook = Path(self.main.PLAYBOOKS / "cluster-reconcile.yml").read_text()
        self.assertIn("owner: '1000'", playbook)
        self.assertIn("'fleet-server', 'elastic-agent'", playbook)
        self.assertIn("assignment.role == 'kibana'", playbook)
        self.assertIn("xpack.fleet.agents.elasticsearch.hosts", playbook)
        self.assertIn("Synchronize and verify the controller-managed Kibana service credential", playbook)
        self.assertIn('"$endpoint/_security/_authenticate"', playbook)
        self.assertIn("Wait for Elasticsearch security index readiness after starting a data role", playbook)
        self.assertIn("/_cluster/health/.security-*?wait_for_status=yellow&wait_for_active_shards=1&timeout=3s", playbook)
        self.assertIn('payload["active_primary_shards"] >= 1', playbook)
        self.assertIn("elasticsearch_data_roles: [hot, warm]", playbook)
        self.assertIn("assignment.role in elasticsearch_data_roles", playbook)
        self.assertIn('ECP_ELASTIC_PASSWORD: "{{ credentials.elastic_password }}"', playbook)
        self.assertIn('ECP_KIBANA_PASSWORD: "{{ credentials.kibana_password }}"', playbook)
        self.assertIn("metricbeat_roles: [master, hot, warm, ml, ingest, coordinating, kibana, logstash]", playbook)
        self.assertIn("Synchronize the Metricbeat monitoring service credential", playbook)
        self.assertIn("remote_monitoring_user", playbook)
        self.assertIn('ECP_MONITORING_PASSWORD: "{{ credentials.monitoring_password }}"', playbook)
        self.assertIn("Render Metricbeat configuration for stack monitoring", playbook)
        metricbeat_data = playbook.split("Grant Metricbeat access to its persistent state directory", 1)[1].split(
            "Reject an unmanaged nonempty data path", 1
        )[0]
        self.assertIn('path: "{{ metricbeat_root }}/data"', metricbeat_data)
        self.assertIn("owner: '1000'", metricbeat_data)
        self.assertIn("mode: '0750'", metricbeat_data)
        metricbeat_config = playbook.split("Render Metricbeat configuration for stack monitoring", 1)[1].split(
            "Render Metricbeat environment", 1
        )[0]
        self.assertIn("mode: '0644'", metricbeat_config)
        self.assertNotIn("credentials.monitoring_password", metricbeat_config)
        metricbeat_environment = playbook.split("Render Metricbeat environment", 1)[1].split(
            "Render Metricbeat Quadlet", 1
        )[0]
        self.assertIn("mode: '0600'", metricbeat_environment)
        self.assertNotIn("User=0\n          Image=docker.elastic.co/beats/metricbeat", playbook)
        self.assertIn("Image=docker.elastic.co/beats/metricbeat:{{ assignment.image_version }}", playbook)
        self.assertIn("Start the requested workload Metricbeat companion", playbook)
        self.assertIn("podman rm -f {{ metricbeat_workload | quote }}", playbook)
        self.assertNotIn("printf 'user = \"elastic:%s\"\\n' {{ credentials.elastic_password | quote }}", playbook)
        self.assertNotIn("printf 'user = \"kibana_system:%s\"\\n' {{ credentials.kibana_password | quote }}", playbook)
        self.assertIn('master: "master,remote_cluster_client"', playbook)
        self.assertIn("IP:{{ membership.data_address }},IP:{{ membership.user_address }}", playbook)
        self.assertIn("basicConstraints=critical,CA:TRUE", playbook)
        self.assertIn("keyUsage=critical,keyCertSign,cRLSign", playbook)
        self.assertIn("ES_SETTING_HTTP_BIND__HOST={{ membership.user_address }}", playbook)
        self.assertIn("ES_SETTING_TRANSPORT_BIND__HOST={{ membership.data_address }}", playbook)
        self.assertIn("Configure Elasticsearch discovery seed hosts", playbook)
        self.assertIn('line: "ES_SETTING_DISCOVERY_SEED__HOSTS={% for master in masters %}', playbook)
        self.assertIn("assignment.id == bootstrap.assignment_id", playbook)
        self.assertIn(
            "Configure initial master election only on the bootstrap master",
            playbook,
        )
        self.assertIn("insertafter: '^ES_SETTING_DISCOVERY_SEED__HOSTS='", playbook)
        self.assertIn('line: "ES_SETTING_CLUSTER_INITIAL__MASTER__NODES={{ bootstrap.workload }}"', playbook)
        https_wait = playbook.split("Wait for Elasticsearch HTTPS readiness", 1)[1].split(
            "Wait for Elasticsearch security index readiness after starting a data role", 1
        )[0]
        self.assertIn('[[ "$code" == 200 || "$code" == 401 ]]', https_wait)
        self.assertNotIn("--config", https_wait)
        self.assertNotIn("ECP_ELASTIC_PASSWORD", https_wait)
        self.assertIn("FLEET_URL=https://{{ membership.user_address }}:{{ assignment.ports.fleet }}", playbook)
        self.assertIn("FLEET_CA=/usr/share/elastic-agent/certs/ca.crt", playbook)
        self.assertIn("json.dumps({\"policy_id\": sys.argv[1]})", playbook)
        self.assertIn('-d "$payload" > agent-token.json', playbook)
        marker = playbook.split("Create managed data marker", 1)[1].split("Create the cluster certificate authority", 1)[0]
        logstash = playbook.split("Render Logstash configuration", 1)[1].split("Render Logstash Quadlet", 1)[0]
        self.assertIn("mode: '0600'", marker)
        self.assertIn('mode: "{{ item.mode }}"', logstash)
        self.assertIn('dest: "{{ config_root }}/logstash.yml"\n          mode: \'0644\'', logstash)
        self.assertIn('dest: "{{ config_root }}/logstash.env"\n          mode: \'0600\'', logstash)
        self.assertIn("Remove empty cluster directories after a scoped purge", playbook)
        self.assertIn("rmdir -- {{ issued_root | quote }}", playbook)
        self.assertNotIn("ES_SETTING_NETWORK_HOST=0.0.0.0", playbook)

    def test_version_observation_and_download_only_api(self):
        headers = self.login()
        node_id = self.node(headers)
        cluster_id = self.cluster(headers)
        self.assertEqual(self.membership(headers, cluster_id, node_id).status_code, 201)
        assignment = self.client.post(f"/api/clusters/{cluster_id}/assignments", headers=headers, json={
            "node_id": node_id, "role": "master", "config": {"cpu": "2", "memory": "4g", "storage_path": "/srv/version/master"},
        }).json()["id"]
        self.main.record_observation({"assignment_id": assignment}, "ECP_VERSION=%s|1|docker.elastic.co/elasticsearch/elasticsearch:8.19.0|sha256:abc|1\n" % assignment, True)
        with patch.object(self.main, "available_versions", return_value=["8.20.0", "8.19.0"]):
            details = self.client.get(f"/api/clusters/{cluster_id}/versions", headers=headers)
            self.assertEqual(details.status_code, 200)
            self.assertEqual(details.json()["assignments"][0]["observation"]["version"], "8.19.0")
            self.assertTrue(details.json()["assignments"][0]["observation"]["cached"])
            with patch.object(self.main, "launch_commands", return_value=42) as launch:
                download = self.client.post(f"/api/clusters/{cluster_id}/versions/download", headers=headers, json={"target_version": "8.20.0"})
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.json()["run_id"], 42)
        self.assertEqual(launch.call_args.args[0], "version-download")

    def test_registry_tags_use_a_bounded_release_window_and_probe_avoids_ansible_templates(self):
        headers = Message()
        self.main.REGISTRY_CACHE.clear()
        with patch.object(self.main, "registry_json", return_value=({"tags": ["8.19.0", "8.20.0-SNAPSHOT", "8.20.0"]}, headers)) as registry:
            self.assertEqual(self.main.registry_tags("elasticsearch/elasticsearch", "8.18.999"), {"8.19.0", "8.20.0"})
        self.assertEqual(registry.call_count, 1)
        self.assertIn("n=100", registry.call_args.args[0])
        self.assertIn("last=8.18.999", registry.call_args.args[0])
        command = self.main.probe_command("inventory", {"slug": "lab"}, {"id": 1, "role": "master", "node_id": 2, "node_name": "node-a"})
        script = command[command.index("-a") + 1]
        self.assertNotIn("{{.Config", script)
        self.assertIn("podman inspect", script)

    def test_upgrade_blocks_single_master_even_with_fresh_versions(self):
        headers = self.login()
        node_id = self.node(headers)
        cluster_id = self.cluster(headers)
        self.assertEqual(self.membership(headers, cluster_id, node_id).status_code, 201)
        assignment = self.client.post(f"/api/clusters/{cluster_id}/assignments", headers=headers, json={
            "node_id": node_id, "role": "master", "config": {"cpu": "2", "memory": "4g", "storage_path": "/srv/upgrade/master"},
        }).json()["id"]
        self.main.record_observation({"assignment_id": assignment}, "ECP_VERSION=%s|1|docker.elastic.co/elasticsearch/elasticsearch:8.19.0|sha256:abc\n" % assignment, True)
        with patch.object(self.main, "available_versions", return_value=["8.20.0"]):
            result = self.client.post(f"/api/clusters/{cluster_id}/upgrades", headers=headers, json={"target_version": "8.20.0"})
        self.assertEqual(result.status_code, 422)
        self.assertIn("three healthy master-eligible", result.json()["detail"])

    def test_upgrade_is_blocked_by_an_active_maintenance_plan_before_preflight(self):
        from app.modules.maintenance.store import MaintenanceRepository

        headers = self.login()
        node_id = self.node(headers)
        cluster_id = self.cluster(headers)
        self.assertEqual(self.membership(headers, cluster_id, node_id).status_code, 201)
        with self.main.db() as connection:
            plan = MaintenanceRepository(connection).create_plan(
                operation_kind="upgrade",
                plan={"target": {"operation": "upgrade", "cluster_id": cluster_id}},
                observation={},
                idempotency_key="upgrade-maintenance-conflict",
                requested_by="operator",
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
                target_cluster_id=cluster_id,
                initial_state="ready",
            )
        try:
            with patch.object(self.main, "available_versions", return_value=["8.20.0"]):
                result = self.client.post(
                    f"/api/clusters/{cluster_id}/upgrades",
                    headers=headers,
                    json={"target_version": "8.20.0"},
                )
        finally:
            with self.main.db() as connection:
                connection.execute("DELETE FROM maintenance_plans WHERE id=?", (plan.id,))
        self.assertEqual(result.status_code, 409)
        self.assertIn("maintenance", result.json()["detail"].lower())

    def test_upgrade_persists_a_fail_closed_maintenance_plan_without_launching_remote_work(self):
        headers = self.login()
        cluster_id = self.cluster(headers)
        assignments = []
        for index in range(3):
            node_id = self.node(
                headers,
                name=f"upgrade-master-{index}",
                address=f"192.0.2.{110 + index}",
            )
            self.assertEqual(
                self.membership(
                    headers,
                    cluster_id,
                    node_id,
                    user_address=f"192.0.2.{110 + index}",
                    data_address=f"198.51.100.{110 + index}",
                ).status_code,
                201,
            )
            assignment = self.client.post(f"/api/clusters/{cluster_id}/assignments", headers=headers, json={
                "node_id": node_id,
                "role": "master",
                "config": {"cpu": "2", "memory": "4g", "storage_path": f"/srv/upgrade/master-{index}"},
            }).json()["id"]
            assignments.append(assignment)
            self.main.record_observation(
                {"assignment_id": assignment},
                "ECP_VERSION=%s|1|docker.elastic.co/elasticsearch/elasticsearch:8.19.0|sha256:%s\n"
                % (assignment, "a" * 64),
                True,
            )
        digest_map = {assignment: "sha256:" + "b" * 64 for assignment in assignments}
        body = None
        try:
            with (
                patch.object(self.main, "available_versions", return_value=["8.20.0"]),
                patch.object(self.main, "target_image_digests", return_value=digest_map),
                patch.object(self.main, "launch_upgrade") as launch,
            ):
                result = self.client.post(
                    f"/api/clusters/{cluster_id}/upgrades",
                    headers=headers,
                    json={"target_version": "8.20.0"},
                )
            self.assertEqual(result.status_code, 200)
            body = result.json()
            self.assertIn("run_id", body)
            self.assertIn("plan_id", body)
            self.assertFalse(body["execution_enabled"])
            self.assertIn("maintenance_upgrade_execution_disabled", body["blockers"])
            launch.assert_not_called()
            with self.main.db() as connection:
                plan = connection.execute(
                    "SELECT lifecycle_state,target_manifest_json FROM maintenance_plans WHERE id=?",
                    (body["plan_id"],),
                ).fetchone()
                run = connection.execute("SELECT status,command_json FROM runs WHERE id=?", (body["run_id"],)).fetchone()
            self.assertEqual(plan["lifecycle_state"], "blocked")
            self.assertIn(body["manifest_hash"], plan["target_manifest_json"])
            self.assertEqual((run["status"], run["command_json"]), ("succeeded", "[]"))
        finally:
            if body:
                with self.main.db() as connection:
                    connection.execute("DELETE FROM maintenance_plans WHERE id=?", (body["plan_id"],))
                    connection.execute("DELETE FROM runs WHERE id=?", (body["run_id"],))

    def test_version_rendering_and_download_only_are_present(self):
        playbook = Path(self.main.PLAYBOOKS / "cluster-reconcile.yml").read_text()
        preflight = Path(self.main.PLAYBOOKS / "cluster-upgrade-preflight.yml").read_text()
        self.assertIn("Image=docker.elastic.co/elasticsearch/elasticsearch:{{ assignment.image_version }}", playbook)
        self.assertIn("successful Elasticsearch snapshot from the last 24 hours", preflight)
        markup, source = self.frontend_markup_and_source()
        self.assertIn('type="module"', markup)
        self.assertIn("Download only", source)
        self.assertIn("versions/download", source)
        self.assertIn("not cached", source)
        self.assertIn("observed_at", source)

    def test_frontend_uses_in_page_dialogs_not_browser_dialogs(self):
        markup, source = self.frontend_markup_and_source()
        self.assertNotIn("app.js", markup)
        self.assertIn('id="root"', markup)
        self.assertIn("Type PURGE to continue", source)
        self.assertIn("Edit network", source)
        self.assertIn("network_mode", source)
        self.assertEqual(self.client.get("/dashboard").status_code, 200)

    def test_root_redirects_to_dashboard(self):
        result = self.client.get("/", follow_redirects=False)
        self.assertEqual(result.status_code, 307)
        self.assertEqual(result.headers["location"], "/dashboard")

    def test_legacy_assignments_are_removed_on_startup(self):
        with self.main.db() as con:
            con.execute("INSERT INTO nodes(name,address,ssh_port,ssh_user,enabled) VALUES ('legacy','192.0.2.104',22,'root',1)")
            con.execute("INSERT INTO assignments(node_id,role,config_json) VALUES (1,'master','{}')")
        self.main.init()
        with self.main.db() as con:
            self.assertEqual(con.execute("SELECT count(*) FROM assignments").fetchone()[0], 0)

    def test_startup_removes_abandoned_run_artifacts(self):
        inventory = self.main.INVENTORIES / "run-stale.yaml"
        variables = self.main.VARIABLES / "run-stale.yaml"
        inventory.write_text("all: {}\n")
        variables.write_text("credentials: stale\n")
        with self.main.db() as con:
            run_id = con.execute(
                "INSERT INTO runs(kind,target,status,command_json) VALUES ('reconcile','stale','running','[]')"
            ).lastrowid

        self.main.init()

        self.assertFalse(inventory.exists())
        self.assertFalse(variables.exists())
        with self.main.db() as con:
            status = con.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()["status"]
        self.assertEqual(status, "failed")


if __name__ == "__main__":
    unittest.main()
