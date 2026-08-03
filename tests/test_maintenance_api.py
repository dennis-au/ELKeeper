from __future__ import annotations

import importlib
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
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

    def cluster(self, headers):
        response = self.client.post("/api/clusters", headers=headers, json={"name": "cluster-a"})
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

    def test_capabilities_are_authenticated_and_disabled_by_default(self):
        self.assertEqual(self.client.get("/api/maintenance/capabilities").status_code, 401)
        response = self.client.get("/api/maintenance/capabilities", headers=self.login())
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["planning"])
        self.assertFalse(response.json()["operations"]["host_reboot"])

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
        for action in ("execute", "pause", "resume", "cancel", "recover"):
            response = self.client.post(f"/api/maintenance/plans/not-a-plan/{action}", headers=headers)
            self.assertEqual(response.status_code, 409)
            self.assertIn("disabled", response.json()["detail"].lower())

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
        from app.maintenance_store import MaintenanceRepository

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


if __name__ == "__main__":
    unittest.main()
