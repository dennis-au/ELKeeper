from __future__ import annotations

import importlib
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient


class MaintenanceApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        os.environ.update(
            APP_DATA_DIR=cls.temp.name,
            APP_RUNTIME_DIR=os.path.join(cls.temp.name, "runtime"),
            APP_SECRET_KEY="test-secret",
            ADMIN_USERNAME="operator",
            ADMIN_PASSWORD="test-password",
            MAINTENANCE_PLANNING_ENABLED="0",
            MAINTENANCE_HOST_REBOOT_ENABLED="0",
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
        from app.maintenance_execution import MAINTENANCE_ADAPTERS

        MAINTENANCE_ADAPTERS.clear()
        self.main.MAINTENANCE_CAPABILITIES.update({
            "planning": False,
            "manual_maintenance_entry": False,
            "container_stop": False,
            "host_shutdown": False,
            "manual_maintenance_exit": True,
            "recovery": True,
            "host_reboot": False,
            "rolling_restart": False,
            "upgrade": False,
            "evacuation": False,
            "node_shutdown_backend": False,
        })
        self.main.console.telemetry.host_states.clear()
        self.main.console.telemetry.cluster_states.clear()
        with self.main.db() as connection:
            for table in (
                "maintenance_locks", "maintenance_checkpoints", "maintenance_steps",
                "maintenance_plans", "maintenance_policies", "host_maintenance_state",
                "workload_change_batches", "workload_observations", "cluster_assignments",
                "memberships", "clusters", "host_runtime_observations", "nodes",
                "runs", "audit_events",
            ):
                connection.execute(f"DELETE FROM {table}")

    def login(self):
        response = self.client.post("/api/auth/login", json={
            "username": "operator", "password": "test-password",
        })
        self.assertEqual(response.status_code, 200)
        return {"Authorization": "Bearer " + response.json()["token"]}

    def node(self, headers, name="node-a"):
        response = self.client.post("/api/nodes", headers=headers, json={
            "name": name, "address": "192.0.2.101", "ssh_port": 22,
            "ssh_user": "root", "enabled": True,
        })
        self.assertEqual(response.status_code, 201)
        return response.json()["id"]

    def cluster(self, headers, name="cluster-a"):
        response = self.client.post("/api/clusters", headers=headers, json={"name": name})
        self.assertEqual(response.status_code, 201)
        return response.json()["id"]

    def observe_empty_host(self, node_id):
        observed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        state = {
            "node_id": node_id,
            "reachable": True,
            "initialized": True,
            "podman_socket_active": True,
            "os_name": "Test Linux",
            "podman_version": "5.8.5",
            "observed_at": observed_at,
            "last_error": "",
            "containers": [],
            "pods": [],
        }
        self.main.console.telemetry.host_states[node_id] = state
        with self.main.db() as connection:
            connection.execute(
                "INSERT INTO host_runtime_observations("
                "node_id,initialized,reachable,podman_socket_active,os_name,podman_version,observed_at,last_error"
                ") VALUES(?,?,?,?,?,?,?,?)",
                (node_id, 1, 1, 1, "Test Linux", "5.8.5", observed_at, ""),
            )

    def active_assignment(self, cluster_id, node_id, *, role):
        with self.main.db() as connection:
            connection.execute(
                "INSERT INTO memberships(cluster_id,node_id,network_mode,data_interface,data_address,user_interface,user_address) "
                "VALUES(?,?,?,?,?,?,?)",
                (cluster_id, node_id, "shared", "ens18", "192.0.2.101", "ens18", "192.0.2.101"),
            )
            assignment_id = connection.execute(
                "INSERT INTO cluster_assignments(cluster_id,node_id,role,config_json,state) VALUES(?,?,?,?, 'active')",
                (cluster_id, node_id, role, self.main.seal_config("{}")),
            ).lastrowid
            connection.execute(
                "INSERT INTO workload_observations(assignment_id,image,digest,version,running,cached,error) VALUES(?,?,?,?,?,?,?)",
                (assignment_id, f"example/{role}:8.19.0", "sha256:" + "a" * 64, "8.19.0", 1, 1, ""),
            )
            return assignment_id

    def evacuation_inventory(self, headers, *, provider="native_podman", same_zone=False):
        """Create durable preview evidence without invoking any managed host."""

        cluster_id = self.cluster(headers)
        source_id = self.node(headers, "evacuation-source")
        replacement_id = self.node(headers, "evacuation-replacement")
        source_network = {"ens18": ["192.0.2.101"]}
        replacement_network = {"ens18": ["192.0.2.102"]}
        with self.main.db() as connection:
            connection.execute("UPDATE clusters SET provider_type=? WHERE id=?", (provider, cluster_id))
            connection.execute("UPDATE nodes SET zone_id=? WHERE id=?", ("zone-a", source_id))
            connection.execute(
                "UPDATE nodes SET zone_id=? WHERE id=?",
                ("zone-a" if same_zone else "zone-b", replacement_id),
            )
            for node_id, address in ((source_id, "192.0.2.101"), (replacement_id, "192.0.2.102")):
                connection.execute(
                    "INSERT INTO memberships(cluster_id,node_id,network_mode,data_interface,data_address,user_interface,user_address) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (cluster_id, node_id, "shared", "ens18", address, "ens18", address),
                )
            assignment_id = connection.execute(
                "INSERT INTO cluster_assignments(cluster_id,node_id,role,config_json,state) VALUES(?,?,?,?, 'active')",
                (
                    cluster_id,
                    source_id,
                    "hot",
                    self.main.seal_config('{"cpu":"1","memory":"2g","storage_path":"/srv/elastic/evacuation"}'),
                ),
            ).lastrowid
            connection.execute(
                "INSERT INTO workload_observations(assignment_id,image,digest,version,running,cached,error) VALUES(?,?,?,?,?,?,?)",
                (assignment_id, "docker.elastic.co/elasticsearch/elasticsearch:8.19.0", "sha256:" + "a" * 64, "8.19.0", 1, 1, ""),
            )
            observed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            for node_id, interfaces in ((source_id, source_network), (replacement_id, replacement_network)):
                connection.execute(
                    "INSERT INTO host_runtime_observations("
                    "node_id,initialized,reachable,podman_socket_active,os_name,podman_version,observed_at,last_error,network_interfaces_json"
                    ") VALUES(?,?,?,?,?,?,?,?,?)",
                    (node_id, 1, 1, 1, "Test Linux", "5.8.5", observed_at, "", __import__("json").dumps(interfaces)),
                )
        return cluster_id, source_id, replacement_id

    def test_capabilities_are_authenticated_and_disabled_by_default(self):
        self.assertEqual(self.client.get("/api/maintenance/capabilities").status_code, 401)
        response = self.client.get("/api/maintenance/capabilities", headers=self.login())
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["planning"])
        self.assertFalse(response.json()["operations"]["manual_maintenance_entry"])
        self.assertFalse(response.json()["operations"]["container_stop"])
        self.assertFalse(response.json()["operations"]["host_shutdown"])
        self.assertTrue(response.json()["lifecycle"]["manual_maintenance_exit"])
        self.assertTrue(response.json()["lifecycle"]["recovery"])
        self.assertFalse(response.json()["operations"]["host_reboot"])

    def test_container_workflow_action_route_is_authenticated_and_fail_closed_by_default(self):
        path = "/api/maintenance/workflows/" + "a" * 32 + "/prepare"
        self.assertEqual(self.client.post(path).status_code, 401)

        disabled = self.client.post(path, headers=self.login())
        self.assertEqual(disabled.status_code, 409, disabled.text)
        self.assertIn("Container maintenance execution is disabled", disabled.text)
        with self.main.db() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 0)

    def test_host_workflow_action_route_is_authenticated_and_fail_closed_by_default(self):
        path = "/api/maintenance/host-workflows/" + "b" * 32 + "/prepare"
        self.assertEqual(self.client.post(path).status_code, 401)

        disabled = self.client.post(path, headers=self.login())
        self.assertEqual(disabled.status_code, 409, disabled.text)
        self.assertIn("Host maintenance execution is disabled", disabled.text)
        with self.main.db() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 0)

    def test_container_workflow_uses_the_attached_run_exact_unit_and_selected_companion(self):
        from app.modules.maintenance.lifecycle import MaintenanceState
        from app.modules.maintenance.store import MaintenanceRepository

        headers = self.login()
        node_id = self.node(headers, "container-maintenance-node")
        cluster_id = self.cluster(headers, "container-maintenance")
        assignment_id = self.active_assignment(cluster_id, node_id, role="kibana")
        self.main.MAINTENANCE_CAPABILITIES["container_stop"] = True
        now = datetime.now(timezone.utc)
        with self.main.db() as connection:
            assignment = connection.execute(
                "SELECT revision FROM cluster_assignments WHERE id=?", (assignment_id,),
            ).fetchone()
            plan = MaintenanceRepository(connection).create_plan(
                operation_kind="workload_restart",
                plan={"policy": {"observation_max_age_seconds": 120}},
                observation={
                    "captured_at": now.isoformat().replace("+00:00", "Z"),
                    "capability_revision": self.main.capability_revision(),
                },
                idempotency_key="container-workflow-main-integration",
                requested_by="operator",
                expires_at=now + timedelta(minutes=5),
                target_node_id=node_id,
                target_cluster_id=cluster_id,
                target_assignment_id=assignment_id,
                target_manifest={
                    "public_operation": "container_maintenance",
                    "affected_cluster_ids": [cluster_id],
                    "assignment_revisions": [{"assignment_id": assignment_id, "revision": assignment["revision"]}],
                },
                initial_state=MaintenanceState.READY,
            )

        remote_calls = []
        companion_calls = []

        async def remote_command(node, *argv, timeout=8):
            remote_calls.append((node["id"], argv, timeout))
            return b""

        def launch_selected(companion_cluster_id, assignment_ids, username):
            companion_calls.append((companion_cluster_id, assignment_ids, username))
            return 91

        with (
            patch.object(self.main.console, "remote_command", new=remote_command),
            patch.object(self.main, "active_ssh_key_path", return_value="/tmp/controller.key"),
            patch.object(self.main, "known_hosts_path", return_value="/tmp/known-hosts"),
            patch.object(
                self.main,
                "ssh_host_key_args",
                return_value=("UserKnownHostsFile=/tmp/known-hosts", "StrictHostKeyChecking=yes"),
            ),
            patch.object(self.main, "launch_filebeat_assignment_reconcile", side_effect=launch_selected),
        ):
            prepared = self.client.post(
                f"/api/maintenance/workflows/{plan.id}/prepare", headers=headers,
            )
            self.assertEqual(prepared.status_code, 200, prepared.text)
            run_id = prepared.json()["run_id"]
            self.assertEqual(prepared.json()["workflow_state"], "ready_to_stop")

            stopped = self.client.post(
                f"/api/maintenance/workflows/{plan.id}/stop", headers=headers,
            )
            self.assertEqual(stopped.status_code, 200, stopped.text)
            self.assertEqual(stopped.json()["run_id"], run_id)

            returned = self.client.post(
                f"/api/maintenance/workflows/{plan.id}/return", headers=headers,
            )
            self.assertEqual(returned.status_code, 200, returned.text)
            self.assertEqual(returned.json()["lifecycle_state"], "succeeded")

        unit = f"ecp-container-maintenance-kibana-{node_id}.service"
        self.assertEqual(remote_calls, [
            (node_id, ("systemctl", "stop", "--", unit), 120),
            (node_id, ("systemctl", "start", "--", unit), 120),
            (node_id, ("systemctl", "is-active", "--quiet", unit), 15),
        ])
        self.assertEqual(companion_calls, [(cluster_id, (assignment_id,), "system")])
        with self.main.db() as connection:
            run = connection.execute("SELECT status,log FROM runs WHERE id=?", (run_id,)).fetchone()
            claim = connection.execute(
                "SELECT operation_run_id FROM cluster_assignments WHERE id=?", (assignment_id,),
            ).fetchone()
        self.assertEqual(run["status"], "succeeded")
        self.assertIn("Preparing selected managed workload.\n", run["log"])
        self.assertIn("Scheduling selected workload companion reconciliation.\n", run["log"])
        self.assertIsNone(claim["operation_run_id"])

    def test_host_workflow_uses_one_run_and_only_manages_selected_host_workloads(self):
        from app.modules.maintenance.lifecycle import MaintenanceState
        from app.modules.maintenance.post_return import HostMaintenancePostReturnResult
        from app.modules.maintenance.store import MaintenanceRepository

        headers = self.login()
        node_id = self.node(headers, "host-maintenance-node")
        cluster_id = self.cluster(headers, "host-maintenance")
        master_id = self.active_assignment(cluster_id, node_id, role="master")
        self.main.MAINTENANCE_CAPABILITIES["host_reboot"] = True
        with self.main.db() as connection:
            kibana_id = connection.execute(
                "INSERT INTO cluster_assignments(cluster_id,node_id,role,config_json,state) VALUES(?,?,?,?, 'active')",
                (cluster_id, node_id, "kibana", self.main.seal_config("{}")),
            ).lastrowid
            connection.execute(
                "INSERT INTO workload_observations(assignment_id,image,digest,version,running,cached,error) VALUES(?,?,?,?,?,?,?)",
                (kibana_id, "example/kibana:8.19.0", "sha256:" + "b" * 64, "8.19.0", 1, 1, ""),
            )
            revisions = connection.execute(
                "SELECT id,revision FROM cluster_assignments WHERE id IN (?,?) ORDER BY id",
                (master_id, kibana_id),
            ).fetchall()
            now = datetime.now(timezone.utc)
            plan = MaintenanceRepository(connection).create_plan(
                operation_kind="reboot",
                plan={"policy": {"observation_max_age_seconds": 120}},
                observation={
                    "captured_at": now.isoformat().replace("+00:00", "Z"),
                    "capability_revision": self.main.capability_revision(),
                },
                idempotency_key="host-workflow-main-integration",
                requested_by="operator",
                expires_at=now + timedelta(minutes=5),
                target_node_id=node_id,
                target_cluster_id=cluster_id,
                target_manifest={
                    "public_operation": "host_maintenance",
                    "affected_cluster_ids": [cluster_id],
                    "assignment_revisions": [
                        {"assignment_id": row["id"], "revision": row["revision"]}
                        for row in revisions
                    ],
                    "post_return_expectations": {
                        "endpoints": [],
                        "clusters": [{
                            "cluster_id": cluster_id,
                            "required_health": "green",
                            "nodes": [{
                                "cluster_id": cluster_id,
                                "assignment_id": master_id,
                                "persistent_node_id": "persistent-master-1",
                                "node_name": f"ecp-host-maintenance-master-{node_id}",
                                "version": "8.19.0",
                                "cluster_uuid": "cluster_uuid_123",
                            }],
                        }],
                        "service_budgets": [],
                    },
                },
                initial_state=MaintenanceState.READY,
            )

        remote_calls = []
        companion_calls = []
        boot_id = b"11111111-1111-1111-1111-111111111111\n"

        async def remote_command(node, *argv, timeout=8):
            remote_calls.append((node["id"], argv, timeout))
            if argv == ("cat", "/proc/sys/kernel/random/boot_id"):
                return boot_id
            return b""

        def launch_selected(companion_cluster_id, assignment_ids, username):
            companion_calls.append((companion_cluster_id, assignment_ids, username))
            return 93

        class VerifiedPostReturn:
            def __init__(self):
                self.requests = []

            async def verify_host_maintenance(self, request):
                self.requests.append(request)
                return HostMaintenancePostReturnResult(
                    state="complete",
                    checks=(),
                    error_categories=(),
                )

            async def aclose(self):
                return None

        post_return = VerifiedPostReturn()

        class VerifiedRebooter:
            def __init__(self):
                self.calls = []

            async def reboot(self, *, plan, targets):
                self.calls.append(("reboot", plan.id, tuple(target.unit for target in targets)))
                return self.main.RuntimeActionResult(confirmed=True, detail="reboot-verified")

            async def cleanup(self, *, plan):
                self.calls.append(("cleanup", plan.id, ()))
                return self.main.RuntimeActionResult(confirmed=True, detail="cleanup-verified")

        rebooter = VerifiedRebooter()
        rebooter.main = self.main

        with (
            patch.object(self.main.console, "remote_command", new=remote_command),
            patch.object(self.main, "active_ssh_key_path", return_value="/tmp/controller.key"),
            patch.object(self.main, "known_hosts_path", return_value="/tmp/known-hosts"),
            patch.object(
                self.main,
                "ssh_host_key_args",
                return_value=("UserKnownHostsFile=/tmp/known-hosts", "StrictHostKeyChecking=yes"),
            ),
            patch.object(self.main, "launch_filebeat_assignment_reconcile", side_effect=launch_selected),
            patch.object(self.main, "_host_post_return_verifier", return_value=post_return),
            patch.object(
                self.main,
                "_host_maintenance_rebooter_factory",
                return_value=lambda _plan, _targets: rebooter,
            ),
        ):
            prepared = self.client.post(
                f"/api/maintenance/host-workflows/{plan.id}/prepare", headers=headers,
            )
            self.assertEqual(prepared.status_code, 200, prepared.text)
            run_id = prepared.json()["run_id"]
            self.assertEqual(prepared.json()["workflow_state"], "ready_to_stop")

            stopped = self.client.post(
                f"/api/maintenance/host-workflows/{plan.id}/stop", headers=headers,
            )
            self.assertEqual(stopped.status_code, 200, stopped.text)
            self.assertEqual(stopped.json()["workflow_state"], "maintenance")

            rebooted = self.client.post(
                f"/api/maintenance/host-workflows/{plan.id}/reboot", headers=headers,
            )
            self.assertEqual(rebooted.status_code, 200, rebooted.text)
            self.assertEqual(rebooted.json()["action"], "reboot")

            returned = self.client.post(
                f"/api/maintenance/host-workflows/{plan.id}/return", headers=headers,
            )
            self.assertEqual(returned.status_code, 200, returned.text)
            self.assertEqual(returned.json()["run_id"], run_id)
            self.assertEqual(returned.json()["lifecycle_state"], "succeeded")

        self.assertEqual([item.assignment_id for item in post_return.requests[0].workloads], [kibana_id, master_id])
        self.assertEqual(post_return.requests[0].clusters[0].nodes[0].persistent_node_id, "persistent-master-1")

        master_unit = f"ecp-host-maintenance-master-{node_id}.service"
        kibana_unit = f"ecp-host-maintenance-kibana-{node_id}.service"
        self.assertEqual(remote_calls, [
            (node_id, ("systemctl", "stop", "--", kibana_unit), 120),
            (node_id, ("systemctl", "stop", "--", master_unit), 120),
            (node_id, ("true",), 30),
            (node_id, ("systemctl", "is-active", "--quiet", "podman.socket"), 15),
            (node_id, ("test", "-d", "/run/systemd/generator"), 15),
            (node_id, ("cat", "/proc/sys/kernel/random/boot_id"), 8),
            (node_id, ("systemctl", "start", "--", master_unit), 120),
            (node_id, ("systemctl", "is-active", "--quiet", master_unit), 15),
            (node_id, ("systemctl", "start", "--", kibana_unit), 120),
            (node_id, ("systemctl", "is-active", "--quiet", kibana_unit), 15),
        ])
        self.assertEqual(companion_calls, [
            (cluster_id, (master_id,), "system"),
            (cluster_id, (kibana_id,), "system"),
        ])
        self.assertEqual(rebooter.calls, [
            ("reboot", plan.id, (master_unit, kibana_unit)),
            ("cleanup", plan.id, ()),
        ])
        with self.main.db() as connection:
            run = connection.execute("SELECT status,log FROM runs WHERE id=?", (run_id,)).fetchone()
            claims = connection.execute(
                "SELECT id,operation_run_id FROM cluster_assignments WHERE id IN (?,?) ORDER BY id",
                (master_id, kibana_id),
            ).fetchall()
        self.assertEqual(run["status"], "succeeded")
        self.assertIn("Preparing managed workloads on the selected host.\n", run["log"])
        self.assertIn("Staging the signed host reboot executor.\n", run["log"])
        self.assertIn("Host reboot and boot transition were verified.\n", run["log"])
        self.assertEqual([(row["id"], row["operation_run_id"]) for row in claims], [
            (master_id, None),
            (kibana_id, None),
        ])

    def test_host_rebooter_factory_binds_the_attached_run_and_exact_workload_units(self):
        from app.modules.maintenance.container_maintenance import ManagedContainerTarget
        from app.modules.maintenance.execution import MaintenanceAction, MaintenanceExecutionService
        from app.modules.maintenance.lifecycle import MaintenanceState
        from app.modules.maintenance.store import MaintenanceRepository

        headers = self.login()
        node_id = self.node(headers, "host-rebooter-factory-node")
        cluster_id = self.cluster(headers, "host-rebooter-factory")
        assignment_id = self.active_assignment(cluster_id, node_id, role="master")
        now = datetime.now(timezone.utc)

        with self.main.db() as connection:
            repository = MaintenanceRepository(connection)
            revision = connection.execute(
                "SELECT revision FROM cluster_assignments WHERE id=?", (assignment_id,),
            ).fetchone()["revision"]
            created = repository.create_plan(
                operation_kind="reboot",
                plan={"policy": {}},
                observation={
                    "captured_at": now.isoformat().replace("+00:00", "Z"),
                    "capability_revision": self.main.capability_revision(),
                },
                idempotency_key="host-rebooter-factory",
                requested_by="operator",
                expires_at=now + timedelta(minutes=5),
                target_node_id=node_id,
                target_cluster_id=cluster_id,
                target_manifest={
                    "public_operation": "host_maintenance",
                    "assignment_revisions": [{"assignment_id": assignment_id, "revision": revision}],
                },
                initial_state=MaintenanceState.READY,
            )
            MaintenanceExecutionService(
                repository,
                capability_revision=self.main.capability_revision,
            ).prepare(created.id, MaintenanceAction.EXECUTE, username="operator")
            plan = repository.get_plan(created.id)
            target = ManagedContainerTarget(
                assignment_id=assignment_id,
                cluster_id=cluster_id,
                node_id=node_id,
                role="master",
                unit=f"ecp-host-rebooter-factory-master-{node_id}.service",
                data_bearing=False,
            )

            rebooter = self.main._host_maintenance_rebooter_factory(connection, repository)(
                plan,
                (target,),
            )

        self.assertEqual(rebooter.runtime.node_id, node_id)
        self.assertTrue(rebooter.orchestrator.execution_enabled)
        self.assertEqual(rebooter.orchestrator.sequence_base, 5000)
        self.assertEqual(rebooter.orchestrator.predicates.expected_assignment_ids, (assignment_id,))
        runner = rebooter.runtime.io.ansible_runner
        self.assertEqual(runner.run_id, plan.run_id)
        self.assertEqual(runner.allowed_playbooks, frozenset({
            "host-maintenance-executor-stage.yml",
            "host-maintenance-reboot.yml",
        }))

    def test_host_post_return_resolver_uses_cached_ca_and_redacts_monitoring_key(self):
        headers = self.login()
        node_id = self.node(headers, "post-return-resolver-node")
        cluster_id = self.cluster(headers, "post-return-resolver")
        self.active_assignment(cluster_id, node_id, role="master")
        raw_key = "ApiKey resolver-key-material"
        ca_path = self.main.cluster_ca_path(self.main.console.CA_CACHE, cluster_id)
        ca_path.parent.mkdir(parents=True, exist_ok=True)
        ca_path.write_text("test-ca", encoding="ascii")

        with self.main.db() as connection:
            credentials = self.main.open_config(
                connection.execute(
                    "SELECT secrets_json FROM clusters WHERE id=?", (cluster_id,),
                ).fetchone()["secrets_json"],
            )
            credentials["monitoring_api_key"] = raw_key
            connection.execute(
                "UPDATE clusters SET secrets_json=? WHERE id=?",
                (self.main.seal_config(json.dumps(credentials)), cluster_id),
            )
            resolved = self.main._HostPostReturnElasticsearchResolver(connection).resolve(cluster_id)

        self.assertEqual(resolved.endpoint, "https://192.0.2.101:9200")
        self.assertEqual(resolved.ca_path, str(ca_path))
        self.assertEqual(resolved.api_key.get_secret_value(), "resolver-key-material")
        self.assertNotIn("resolver-key-material", repr(resolved))

        ca_path.unlink()
        with self.main.db() as connection:
            with self.assertRaisesRegex(RuntimeError, "CA material") as raised:
                self.main._HostPostReturnElasticsearchResolver(connection).resolve(cluster_id)
        self.assertNotIn("resolver-key-material", str(raised.exception))

    def test_managed_endpoint_probe_targets_are_exact_and_skip_malformed_runtime_addresses(self):
        headers = self.login()
        node_id = self.node(headers, "managed-endpoint-targets")
        cluster_id = self.cluster(headers, "managed-endpoint-targets")
        assignment_ids = {"master": self.active_assignment(cluster_id, node_id, role="master")}
        with self.main.db() as connection:
            for role in ("kibana", "fleet-server", "logstash", "hot", "elastic-agent"):
                assignment_id = connection.execute(
                    "INSERT INTO cluster_assignments(cluster_id,node_id,role,config_json,state) "
                    "VALUES(?,?,?,?, 'active')",
                    (cluster_id, node_id, role, self.main.seal_config("{}")),
                ).lastrowid
                connection.execute(
                    "INSERT INTO workload_observations(assignment_id,image,digest,version,running,cached,error) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (assignment_id, f"example/{role}:8.19.0", "sha256:" + "c" * 64, "8.19.0", 1, 1, ""),
                )
                assignment_ids[role] = assignment_id
            targets = self.main._managed_endpoint_probe_targets(connection)

        self.assertEqual(set(targets), {
            f"assignment-{assignment_ids['master']}",
            f"assignment-{assignment_ids['kibana']}",
            f"assignment-{assignment_ids['fleet-server']}",
            f"assignment-{assignment_ids['logstash']}",
        })
        self.assertEqual(
            targets[f"assignment-{assignment_ids['master']}"].url,
            "https://192.0.2.101:9200/",
        )
        self.assertEqual(
            targets[f"assignment-{assignment_ids['kibana']}"].url,
            "https://192.0.2.101:5601/api/status",
        )
        self.assertEqual(
            targets[f"assignment-{assignment_ids['fleet-server']}"].url,
            "https://192.0.2.101:8220/api/status",
        )
        self.assertEqual(
            targets[f"assignment-{assignment_ids['logstash']}"].url,
            "http://192.0.2.101:9600/",
        )
        self.assertIsNotNone(targets[f"assignment-{assignment_ids['master']}"].ca_path)
        self.assertIsNone(targets[f"assignment-{assignment_ids['logstash']}"].ca_path)

        with self.main.db() as connection:
            connection.execute(
                "UPDATE memberships SET user_address=? WHERE cluster_id=? AND node_id=?",
                ("not-a-literal-ip", cluster_id, node_id),
            )
            malformed_targets = self.main._managed_endpoint_probe_targets(connection)
        self.assertEqual(malformed_targets, {})

    def test_data_container_workflow_composes_and_restores_the_allocation_guard(self):
        from app.modules.maintenance.lifecycle import MaintenanceState
        from app.modules.maintenance.store import MaintenanceRepository

        headers = self.login()
        node_id = self.node(headers, "data-maintenance-node")
        cluster_id = self.cluster(headers, "data-maintenance")
        assignment_id = self.active_assignment(cluster_id, node_id, role="hot")
        self.main.MAINTENANCE_CAPABILITIES["container_stop"] = True
        now = datetime.now(timezone.utc)
        with self.main.db() as connection:
            assignment = connection.execute(
                "SELECT revision FROM cluster_assignments WHERE id=?", (assignment_id,),
            ).fetchone()
            plan = MaintenanceRepository(connection).create_plan(
                operation_kind="workload_restart",
                plan={"policy": {"observation_max_age_seconds": 120}},
                observation={
                    "captured_at": now.isoformat().replace("+00:00", "Z"),
                    "capability_revision": self.main.capability_revision(),
                },
                idempotency_key="data-container-workflow-main-integration",
                requested_by="operator",
                expires_at=now + timedelta(minutes=5),
                target_node_id=node_id,
                target_cluster_id=cluster_id,
                target_assignment_id=assignment_id,
                target_manifest={
                    "public_operation": "container_maintenance",
                    "affected_cluster_ids": [cluster_id],
                    "assignment_revisions": [{"assignment_id": assignment_id, "revision": assignment["revision"]}],
                },
                initial_state=MaintenanceState.READY,
            )

        remote_calls = []
        clients = []

        class FakeElasticsearchClient:
            def __init__(self, config, credential):
                self.config = config
                self.credential = credential
                self.layers = {"persistent": {}, "transient": {}}
                self.calls = []
                self.closed = False
                clients.append(self)

            async def settings(self):
                return {key: dict(value) for key, value in self.layers.items()}

            async def put_settings(self, *, persistent=None, transient=None):
                self.calls.append((persistent, transient))
                for layer_name, values in (("persistent", persistent), ("transient", transient)):
                    if values is None:
                        continue
                    for key, value in values.items():
                        if value is None:
                            self.layers[layer_name].pop(key, None)
                        else:
                            self.layers[layer_name][key] = value
                return {key: dict(value) for key, value in self.layers.items()}

            async def aclose(self):
                self.closed = True

        async def remote_command(node, *argv, timeout=8):
            remote_calls.append((node["id"], argv, timeout))
            return b""

        with tempfile.TemporaryDirectory() as temporary:
            ca_path = Path(temporary) / "ca.crt"
            ca_path.write_text("test-ca", encoding="ascii")
            with (
                patch.object(self.main.console, "remote_command", new=remote_command),
                patch.object(self.main, "active_ssh_key_path", return_value="/tmp/controller.key"),
                patch.object(self.main, "known_hosts_path", return_value="/tmp/known-hosts"),
                patch.object(
                    self.main,
                    "ssh_host_key_args",
                    return_value=("UserKnownHostsFile=/tmp/known-hosts", "StrictHostKeyChecking=yes"),
                ),
                patch.object(self.main, "launch_filebeat_assignment_reconcile", return_value=92),
                patch.object(self.main, "cluster_ca_path", return_value=ca_path),
                patch.object(self.main, "ElasticsearchMaintenanceClient", FakeElasticsearchClient),
            ):
                self.assertEqual(
                    self.client.post(f"/api/maintenance/workflows/{plan.id}/prepare", headers=headers).status_code,
                    200,
                )
                self.assertEqual(
                    self.client.post(f"/api/maintenance/workflows/{plan.id}/stop", headers=headers).status_code,
                    200,
                )
                returned = self.client.post(f"/api/maintenance/workflows/{plan.id}/return", headers=headers)
                self.assertEqual(returned.status_code, 200, returned.text)

        self.assertEqual(len(clients), 2)
        self.assertEqual(clients[0].calls, [({"cluster.routing.allocation.enable": "primaries"}, None)])
        self.assertEqual(clients[1].calls, [
            ({"cluster.routing.allocation.enable": None}, {"cluster.routing.allocation.enable": None}),
        ])
        self.assertTrue(all(client.closed for client in clients))
        self.assertEqual(remote_calls[0][1][:3], ("systemctl", "stop", "--"))
        with self.main.db() as connection:
            guard = connection.execute(
                "SELECT phase FROM maintenance_allocation_guards WHERE owner_plan_id=?", (plan.id,),
            ).fetchone()
        self.assertEqual(guard["phase"], "restored")

    def test_policy_defaults_and_expected_revision_updates(self):
        headers = self.login()
        cluster_id = self.cluster(headers)

        default = self.client.get(f"/api/clusters/{cluster_id}/maintenance-policy", headers=headers)
        self.assertEqual(default.status_code, 200)
        self.assertEqual(default.json()["revision"], 0)
        self.assertFalse(default.json()["customized"])
        self.assertEqual(default.json()["policy"]["max_unavailable"], 1)

        updated = self.client.put(
            f"/api/clusters/{cluster_id}/maintenance-policy",
            headers=headers,
            json={"expected_revision": 0, "policy": {"max_unavailable": 2}},
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["revision"], 1)
        self.assertTrue(updated.json()["customized"])
        self.assertEqual(updated.json()["policy"]["max_unavailable"], 2)

        stale = self.client.put(
            f"/api/clusters/{cluster_id}/maintenance-policy",
            headers=headers,
            json={"expected_revision": 0, "policy": {"max_unavailable": 3}},
        )
        self.assertEqual(stale.status_code, 409)

    def test_provider_metadata_is_versioned_and_unverified_clusters_are_read_only(self):
        headers = self.login()
        cluster_id = self.cluster(headers)

        current = self.client.get(f"/api/clusters/{cluster_id}/provider", headers=headers)
        self.assertEqual(current.status_code, 200, current.text)
        self.assertEqual(current.json()["provider_type"], "native_podman")
        self.assertEqual(current.json()["ownership_state"], "verified")
        self.assertTrue(current.json()["capabilities"]["workload_mutation"])

        updated = self.client.put(
            f"/api/clusters/{cluster_id}/provider",
            headers=headers,
            json={
                "expected_revision": 1,
                "provider_type": "adopted_podman",
                "ownership_state": "unverified",
                "maintenance_backend": "documented_rolling",
                "capability_overrides": {},
                "connection_references": {"provider_resource_id": "import-7"},
                "expected_cluster_uuid": "ClusterUuid_1234",
            },
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["revision"], 2)
        self.assertFalse(updated.json()["capabilities"]["workload_mutation"])

        blocked = self.client.put(
            f"/api/clusters/{cluster_id}",
            headers=headers,
            json={"name": "cluster-a"},
        )
        self.assertEqual(blocked.status_code, 409, blocked.text)
        self.assertIn("cluster_settings", blocked.text)

        stale = self.client.put(
            f"/api/clusters/{cluster_id}/provider",
            headers=headers,
            json={
                "expected_revision": 1,
                "provider_type": "adopted_podman",
                "ownership_state": "verified",
                "maintenance_backend": "documented_rolling",
            },
        )
        self.assertEqual(stale.status_code, 409)

    def test_endpoint_only_provider_rejects_mutating_backend(self):
        headers = self.login()
        cluster_id = self.cluster(headers)
        response = self.client.put(
            f"/api/clusters/{cluster_id}/provider",
            headers=headers,
            json={
                "expected_revision": 1,
                "provider_type": "eck_endpoint",
                "ownership_state": "verified",
                "maintenance_backend": "documented_rolling",
            },
        )
        self.assertEqual(response.status_code, 422, response.text)

    def test_host_preview_is_gated_idempotent_and_non_mutating(self):
        headers = self.login()
        node_id = self.node(headers)
        self.observe_empty_host(node_id)
        request = {
            "operation": "reboot",
            "reason": "Operating-system maintenance",
            "availability_mode": "zero-impact",
            "idempotency_key": "host-preview-request-1",
        }

        disabled = self.client.post(
            f"/api/nodes/{node_id}/maintenance/plans", headers=headers, json=request,
        )
        self.assertEqual(disabled.status_code, 409)

        self.main.MAINTENANCE_CAPABILITIES["planning"] = True
        first = self.client.post(
            f"/api/nodes/{node_id}/maintenance/plans", headers=headers, json=request,
        )
        self.assertEqual(first.status_code, 201, first.text)
        payload = first.json()
        self.assertEqual(payload["lifecycle_state"], "ready")
        self.assertEqual(payload["view"]["header"]["target"]["name"], "node-a")
        self.assertEqual(payload["view"]["header"]["freshness"]["state"], "fresh")
        self.assertEqual(payload["view"]["impact"]["workloads"], [])
        self.assertEqual(payload["operation"]["progress"]["lifecycleState"], "ready")
        self.assertEqual(payload["operation"]["progress"]["hostBoot"]["state"], "not_started")
        self.assertEqual(payload["operation"]["action_controls"], {})

        second = self.client.post(
            f"/api/nodes/{node_id}/maintenance/plans", headers=headers, json=request,
        )
        self.assertEqual(second.status_code, 201)
        self.assertEqual(second.json()["plan_id"], payload["plan_id"])
        fetched = self.client.get(f"/api/maintenance/plans/{payload['plan_id']}", headers=headers)
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(fetched.json()["plan_hash"], payload["plan_hash"])
        self.assertEqual(fetched.json()["operation"]["progress"]["progress"]["total"], 7)

        with self.main.db() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM maintenance_plans").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM maintenance_locks").fetchone()[0], 0)

    def test_generic_preview_is_gated_idempotent_and_listable(self):
        headers = self.login()
        node_id = self.node(headers)
        self.observe_empty_host(node_id)
        request = {
            "operation": "manual_maintenance",
            "node_id": node_id,
            "reason": "Operator inspection",
            "availability_mode": "zero-impact",
            "idempotency_key": "generic-preview-1",
        }
        disabled = self.client.post("/api/maintenance/plans/preview", headers=headers, json=request)
        self.assertEqual(disabled.status_code, 409)

        self.main.MAINTENANCE_CAPABILITIES["planning"] = True
        first = self.client.post("/api/maintenance/plans/preview", headers=headers, json=request)
        self.assertEqual(first.status_code, 201, first.text)
        payload = first.json()
        self.assertEqual(payload["lifecycle_state"], "ready")
        self.assertEqual(payload["view"]["header"]["target"]["kind"], "host")
        self.assertIn("Manual maintenance", payload["view"]["header"]["operation"])

        second = self.client.post("/api/maintenance/plans/preview", headers=headers, json=request)
        self.assertEqual(second.status_code, 201, second.text)
        self.assertEqual(second.json()["plan_id"], payload["plan_id"])
        listed = self.client.get(
            f"/api/maintenance/plans?host_id={node_id}&state=ready", headers=headers,
        )
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(listed.json()["count"], 1)
        with self.main.db() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM maintenance_locks").fetchone()[0], 0)
        stale_node_id = self.node(headers, name="node-stale")
        stale = self.client.post(
            "/api/maintenance/plans/preview",
            headers=headers,
            json={
                "operation": "manual_maintenance",
                "node_id": stale_node_id,
                "reason": "Stale observation preview",
                "idempotency_key": "generic-preview-stale",
            },
        )
        self.assertEqual(stale.status_code, 201, stale.text)
        self.assertEqual(stale.json()["lifecycle_state"], "blocked")
        self.assertEqual(stale.json()["view"]["header"]["freshness"]["state"], "stale")

    def test_explicit_host_and_container_maintenance_previews_are_read_only_and_isolated(self):
        headers = self.login()
        node_id = self.node(headers)
        first_cluster_id = self.cluster(headers)
        second_cluster_id = self.cluster(headers, "cluster-b")
        first_assignment_id = self.active_assignment(first_cluster_id, node_id, role="hot")
        second_assignment_id = self.active_assignment(second_cluster_id, node_id, role="kibana")
        self.observe_empty_host(node_id)
        self.main.MAINTENANCE_CAPABILITIES["planning"] = True

        host = self.client.post(
            "/api/maintenance/plans/preview",
            headers=headers,
            json={
                "operation": "host_maintenance",
                "node_id": node_id,
                "reason": "Host patching",
                "idempotency_key": "host-maintenance-preview",
            },
        )
        self.assertEqual(host.status_code, 201, host.text)
        self.assertEqual(host.json()["view"]["header"]["target"]["kind"], "host")
        self.assertEqual(host.json()["view"]["header"]["target"]["id"], node_id)
        self.assertEqual({item["id"] for item in host.json()["view"]["impact"]["clusters"]}, {first_cluster_id, second_cluster_id})
        with self.main.db() as connection:
            row = connection.execute(
                "SELECT target_manifest_json FROM maintenance_plans WHERE id=?",
                (host.json()["plan_id"],),
            ).fetchone()
        manifest = json.loads(row["target_manifest_json"])
        self.assertEqual(
            manifest["assignment_revisions"],
            [
                {"assignment_id": first_assignment_id, "revision": 1},
                {"assignment_id": second_assignment_id, "revision": 1},
            ],
        )

        container = self.client.post(
            "/api/maintenance/plans/preview",
            headers=headers,
            json={
                "operation": "container_maintenance",
                "assignment_id": first_assignment_id,
                "reason": "Inspect one workload",
                "idempotency_key": "container-maintenance-preview",
            },
        )
        self.assertEqual(container.status_code, 201, container.text)
        self.assertEqual(container.json()["view"]["header"]["target"]["kind"], "container")
        self.assertEqual(container.json()["view"]["header"]["target"]["id"], first_assignment_id)
        self.assertEqual(
            {item["id"] for item in container.json()["view"]["impact"]["workloads"]},
            {first_assignment_id},
        )
        self.assertNotIn(second_assignment_id, {item["id"] for item in container.json()["view"]["impact"]["workloads"]})
        with self.main.db() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM maintenance_locks").fetchone()[0], 0)

    def test_generic_preview_rejects_bad_target_and_idempotency_conflict(self):
        headers = self.login()
        node_id = self.node(headers)
        self.observe_empty_host(node_id)
        self.main.MAINTENANCE_CAPABILITIES["planning"] = True
        base = {
            "operation": "manual_maintenance",
            "node_id": node_id,
            "reason": "Operator inspection",
            "idempotency_key": "generic-preview-conflict",
        }
        first = self.client.post("/api/maintenance/plans/preview", headers=headers, json=base)
        self.assertEqual(first.status_code, 201, first.text)
        conflict = self.client.post(
            "/api/maintenance/plans/preview",
            headers=headers,
            json={**base, "reason": "Different target intent"},
        )
        self.assertEqual(conflict.status_code, 409, conflict.text)
        missing = self.client.post(
            "/api/maintenance/plans/preview",
            headers=headers,
            json={
                "operation": "resource_change",
                "assignment_ids": [9999],
                "reason": "Resource preview",
            },
        )
        self.assertEqual(missing.status_code, 422, missing.text)

    def test_idempotent_repeat_and_conflict_do_not_recollect_observations(self):
        headers = self.login()
        node_id = self.node(headers)
        self.observe_empty_host(node_id)
        self.main.MAINTENANCE_CAPABILITIES["planning"] = True
        request = {
            "operation": "reboot",
            "reason": "Operating-system maintenance",
            "availability_mode": "zero-impact",
            "idempotency_key": "host-preview-no-recollect",
        }
        first = self.client.post(
            f"/api/nodes/{node_id}/maintenance/plans", headers=headers, json=request,
        )
        self.assertEqual(first.status_code, 201, first.text)

        with patch(
            "app.maintenance_api.collect_host_reboot_planning_data",
            side_effect=AssertionError("idempotent requests must not recollect observations"),
        ):
            repeated = self.client.post(
                f"/api/nodes/{node_id}/maintenance/plans",
                headers=headers,
                json={**request, "reason": "  Operating-system maintenance  "},
            )
            conflict = self.client.post(
                f"/api/nodes/{node_id}/maintenance/plans",
                headers=headers,
                json={**request, "reason": "Different maintenance request"},
            )

        self.assertEqual(repeated.status_code, 201, repeated.text)
        self.assertEqual(repeated.json()["plan_id"], first.json()["plan_id"])
        self.assertEqual(conflict.status_code, 409, conflict.text)
        self.assertNotIn("Operating-system maintenance", conflict.text)

    def test_execution_controls_remain_disabled(self):
        headers = self.login()
        for action in ("execute", "pause", "resume", "cancel"):
            response = self.client.post(f"/api/maintenance/plans/not-a-plan/{action}", headers=headers)
            self.assertEqual(response.status_code, 409)
            self.assertIn("disabled", response.json()["detail"].lower())
        recovery = self.client.post("/api/maintenance/plans/not-a-plan/recover", headers=headers)
        self.assertEqual(recovery.status_code, 404)

    def test_recovery_action_is_available_without_host_execution_capability(self):
        headers = self.login()
        node_id = self.node(headers)
        cluster_id = self.cluster(headers)
        assignment_id = self.active_assignment(cluster_id, node_id, role="kibana")
        with self.main.db() as connection:
            from app.modules.maintenance.api import capability_revision
            from app.modules.maintenance.execution import MaintenanceAction, MaintenanceExecutionService
            from app.modules.maintenance.lifecycle import MaintenanceState
            from app.modules.maintenance.store import MaintenanceRepository

            repository = MaintenanceRepository(connection)
            plan = repository.create_plan(
                operation_kind="workload_restart",
                plan={"policy": {"observation_max_age_seconds": 120}},
                observation={
                    "captured_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "capability_revision": capability_revision(),
                },
                idempotency_key="recover-container-workflow",
                requested_by="operator",
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
                target_node_id=node_id,
                target_cluster_id=cluster_id,
                target_assignment_id=assignment_id,
                target_manifest={
                    "public_operation": "container_maintenance",
                    "affected_cluster_ids": [cluster_id],
                    "assignment_revisions": [{"assignment_id": assignment_id, "revision": 1}],
                },
                initial_state=MaintenanceState.READY,
            )
            ticket = MaintenanceExecutionService(
                repository,
                capability_revision=capability_revision,
            ).prepare(plan.id, MaintenanceAction.EXECUTE, username="operator")

        self.assertFalse(self.main.MAINTENANCE_CAPABILITIES["host_reboot"])
        recovered = self.client.post(
            f"/api/maintenance/plans/{plan.id}/recover",
            headers=headers,
        )
        self.assertEqual(recovered.status_code, 200, recovered.text)
        self.assertEqual(recovered.json()["lifecycle_state"], "recovery_required")
        self.assertEqual(recovered.json()["run_id"], ticket.run_id)
        with self.main.db() as connection:
            self.assertEqual(connection.execute("SELECT status FROM runs WHERE id=?", (ticket.run_id,)).fetchone()["status"], "recovery_required")
            self.assertGreater(connection.execute("SELECT COUNT(*) FROM maintenance_locks WHERE released_at IS NULL").fetchone()[0], 0)

    def test_plan_read_marks_an_expired_active_workflow_for_recovery(self):
        headers = self.login()
        node_id = self.node(headers)
        cluster_id = self.cluster(headers)
        assignment_id = self.active_assignment(cluster_id, node_id, role="kibana")
        with self.main.db() as connection:
            from app.modules.maintenance.api import capability_revision
            from app.modules.maintenance.execution import MaintenanceAction, MaintenanceExecutionService
            from app.modules.maintenance.lifecycle import MaintenanceState
            from app.modules.maintenance.store import MaintenanceRepository

            repository = MaintenanceRepository(connection)
            plan = repository.create_plan(
                operation_kind="workload_restart",
                plan={"policy": {"observation_max_age_seconds": 120}},
                observation={
                    "captured_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "capability_revision": capability_revision(),
                },
                idempotency_key="expired-container-workflow",
                requested_by="operator",
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
                target_node_id=node_id,
                target_cluster_id=cluster_id,
                target_assignment_id=assignment_id,
                target_manifest={
                    "public_operation": "container_maintenance",
                    "affected_cluster_ids": [cluster_id],
                    "assignment_revisions": [{"assignment_id": assignment_id, "revision": 1}],
                },
                initial_state=MaintenanceState.READY,
            )
            ticket = MaintenanceExecutionService(
                repository,
                capability_revision=capability_revision,
            ).prepare(plan.id, MaintenanceAction.EXECUTE, username="operator")
            connection.execute(
                "UPDATE maintenance_plans SET expires_at=? WHERE id=?",
                ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat().replace("+00:00", "Z"), plan.id),
            )

        detail = self.client.get(f"/api/maintenance/plans/{plan.id}", headers=headers)

        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(detail.json()["lifecycle_state"], "recovery_required")
        with self.main.db() as connection:
            self.assertEqual(connection.execute("SELECT status FROM runs WHERE id=?", (ticket.run_id,)).fetchone()["status"], "recovery_required")
            self.assertGreater(connection.execute("SELECT COUNT(*) FROM maintenance_locks WHERE released_at IS NULL").fetchone()[0], 0)

    def test_enabled_execution_fails_closed_without_an_adapter_and_creates_nothing(self):
        headers = self.login()
        node_id = self.node(headers)
        self.observe_empty_host(node_id)
        self.main.MAINTENANCE_CAPABILITIES["planning"] = True
        created = self.client.post(
            f"/api/nodes/{node_id}/maintenance/plans",
            headers=headers,
            json={
                "operation": "reboot",
                "reason": "Operating-system maintenance",
                "availability_mode": "zero-impact",
                "idempotency_key": "missing-adapter",
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        self.main.MAINTENANCE_CAPABILITIES["host_reboot"] = True

        response = self.client.post(
            f"/api/maintenance/plans/{created.json()['plan_id']}/execute",
            headers=headers,
        )

        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("adapter", response.json()["detail"].lower())
        with self.main.db() as connection:
            plan = connection.execute(
                "SELECT lifecycle_state,run_id FROM maintenance_plans WHERE id=?",
                (created.json()["plan_id"],),
            ).fetchone()
            self.assertEqual(tuple(plan), ("ready", None))
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM maintenance_locks").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 0)

    def test_enabled_execution_returns_run_and_dispatches_only_after_validation(self):
        from app.maintenance_execution import AdapterResult, MAINTENANCE_ADAPTERS

        class Adapter:
            def __init__(self):
                self.actions = []

            async def perform(self, request):
                self.actions.append(request.action.value)
                return AdapterResult()

        headers = self.login()
        node_id = self.node(headers)
        self.observe_empty_host(node_id)
        self.main.MAINTENANCE_CAPABILITIES.update({"planning": True, "host_reboot": True})
        created = self.client.post(
            f"/api/nodes/{node_id}/maintenance/plans",
            headers=headers,
            json={
                "operation": "reboot",
                "reason": "Operating-system maintenance",
                "availability_mode": "zero-impact",
                "idempotency_key": "dispatch-adapter",
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        adapter = Adapter()
        MAINTENANCE_ADAPTERS["reboot"] = adapter

        response = self.client.post(
            f"/api/maintenance/plans/{created.json()['plan_id']}/execute",
            headers=headers,
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsInstance(response.json()["run_id"], int)
        self.assertEqual(response.json()["lifecycle_state"], "executing")
        self.assertEqual(adapter.actions, ["execute"])
        with self.main.db() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM maintenance_locks").fetchone()[0], 1)

    def test_expired_plan_blocks_before_adapter_dispatch_or_run_creation(self):
        from app.maintenance_execution import AdapterResult, MAINTENANCE_ADAPTERS

        class Adapter:
            calls = 0

            async def perform(self, request):
                self.calls += 1
                return AdapterResult()

        headers = self.login()
        node_id = self.node(headers)
        self.observe_empty_host(node_id)
        self.main.MAINTENANCE_CAPABILITIES.update({"planning": True, "host_reboot": True})
        created = self.client.post(
            f"/api/nodes/{node_id}/maintenance/plans",
            headers=headers,
            json={
                "operation": "reboot",
                "reason": "Operating-system maintenance",
                "availability_mode": "zero-impact",
                "idempotency_key": "expired-before-dispatch",
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        with self.main.db() as connection:
            connection.execute(
                "UPDATE maintenance_plans SET expires_at=? WHERE id=?",
                ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), created.json()["plan_id"]),
            )
        adapter = Adapter()
        MAINTENANCE_ADAPTERS["reboot"] = adapter

        response = self.client.post(
            f"/api/maintenance/plans/{created.json()['plan_id']}/execute",
            headers=headers,
        )

        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("expired", response.json()["detail"].lower())
        self.assertEqual(adapter.calls, 0)
        with self.main.db() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 0)

    def test_ready_maintenance_plan_blocks_overlapping_legacy_cluster_mutation(self):
        from app.modules.maintenance.store import MaintenanceRepository

        headers = self.login()
        cluster_id = self.cluster(headers)
        with self.main.db() as connection:
            MaintenanceRepository(connection).create_plan(
                operation_kind="settings_change",
                plan={"steps": [{"kind": "verify"}]},
                observation={"cluster_id": cluster_id},
                idempotency_key="legacy-conflict-cluster",
                requested_by="operator",
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
                target_cluster_id=cluster_id,
                initial_state="ready",
            )

        response = self.client.put(
            f"/api/clusters/{cluster_id}", headers=headers, json={"name": "cluster-a"},
        )

        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("maintenance", response.text.lower())

    def test_evacuation_preview_is_authenticated_read_only_and_fail_closed(self):
        headers = self.login()
        cluster_id, source_id, replacement_id = self.evacuation_inventory(headers)
        payload = {"cluster_id": cluster_id, "source_node_id": source_id, "replacement_node_id": replacement_id}
        self.assertEqual(self.client.post("/api/maintenance/evacuation/preview", json=payload).status_code, 401)
        response = self.client.post("/api/maintenance/evacuation/preview", headers=headers, json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["provider"], "native_podman")
        self.assertEqual(response.json()["required_capacity"], 1)
        self.assertIsNone(response.json()["available_capacity"])
        self.assertIn("replacement_capacity_unobserved", response.json()["blockers"])
        self.assertFalse(response.json()["mutation_allowed"])
        self.assertFalse(response.json()["execution_enabled"])
        self.assertFalse(response.json()["capability_enabled"])
        with self.main.db() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM maintenance_plans").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 0)

    def test_evacuation_preview_blocks_endpoint_provider_and_same_node(self):
        headers = self.login()
        cluster_id, source_id, _ = self.evacuation_inventory(headers, provider="eck_endpoint")
        response = self.client.post(
            "/api/maintenance/evacuation/preview",
            headers=headers,
            json={
                "cluster_id": cluster_id,
                "source_node_id": source_id,
                "replacement_node_id": source_id,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        blockers = set(response.json()["blockers"])
        self.assertIn("provider_read_only", blockers)
        self.assertIn("replacement_must_differ_from_source", blockers)
        self.assertFalse(response.json()["mutation_allowed"])

    def test_evacuation_preview_rejects_browser_supplied_provider_and_capacity(self):
        headers = self.login()
        cluster_id, source_id, replacement_id = self.evacuation_inventory(headers)
        response = self.client.post(
            "/api/maintenance/evacuation/preview",
            headers=headers,
            json={
                "cluster_id": cluster_id,
                "source_node_id": source_id,
                "replacement_node_id": replacement_id,
                "provider": "eck_endpoint",
                "available_capacity": 999,
            },
        )
        self.assertEqual(response.status_code, 422, response.text)


if __name__ == "__main__":
    unittest.main()
