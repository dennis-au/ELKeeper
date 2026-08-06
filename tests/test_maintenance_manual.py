from __future__ import annotations

import importlib
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient


class ManualMaintenanceApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        os.environ.update(
            APP_DATA_DIR=cls.temp.name,
            APP_RUNTIME_DIR=os.path.join(cls.temp.name, "runtime"),
            APP_SECRET_KEY="test-secret",
            ADMIN_USERNAME="operator",
            ADMIN_PASSWORD="test-password",
            MAINTENANCE_PLANNING_ENABLED="1",
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
        self.main.MAINTENANCE_CAPABILITIES.update({
            "planning": True,
            "manual_maintenance_entry": True,
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
        with self.main.db() as connection:
            for table in (
                "maintenance_locks", "maintenance_checkpoints", "maintenance_steps",
                "maintenance_plans", "maintenance_policies", "host_maintenance_state",
                "runs", "audit_events", "nodes",
            ):
                connection.execute(f"DELETE FROM {table}")

    def login(self):
        response = self.client.post("/api/auth/login", json={
            "username": "operator", "password": "test-password",
        })
        self.assertEqual(response.status_code, 200, response.text)
        return {"Authorization": "Bearer " + response.json()["token"]}

    def node(self, headers):
        response = self.client.post("/api/nodes", headers=headers, json={
            "name": "manual-node", "address": "192.0.2.101", "ssh_port": 22,
            "ssh_user": "root", "enabled": True,
        })
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["id"]

    def observe(self, node_id, *, healthy=True, observed_at=None):
        observed_at = observed_at or datetime.now(timezone.utc)
        value = observed_at.isoformat().replace("+00:00", "Z")
        self.main.console.telemetry.host_states[node_id] = {
            "node_id": node_id,
            "reachable": healthy,
            "initialized": healthy,
            "podman_socket_active": healthy,
            "observed_at": value,
            "last_error": "" if healthy else "probe failed",
        }
        with self.main.db() as connection:
            connection.execute(
                "INSERT INTO host_runtime_observations("
                "node_id,initialized,reachable,podman_socket_active,os_name,podman_version,observed_at,last_error"
                ") VALUES(?,?,?,?,?,?,?,?)",
                (node_id, int(healthy), int(healthy), int(healthy), "Test Linux", "5.8.5", value, "" if healthy else "probe failed"),
            )

    def test_enter_is_persistent_locked_and_non_mutating(self):
        headers = self.login()
        node_id = self.node(headers)
        response = self.client.post(
            f"/api/nodes/{node_id}/maintenance-mode/enter",
            headers=headers,
            json={"reason": "kernel maintenance", "duration_seconds": 600, "idempotency_key": "manual-1"},
        )
        self.assertEqual(response.status_code, 201, response.text)
        payload = response.json()
        self.assertEqual(payload["state"], "maintenance")
        self.assertIsInstance(payload["run_id"], int)
        with self.main.db() as connection:
            self.assertEqual(tuple(connection.execute("SELECT operation_kind,lifecycle_state FROM maintenance_plans").fetchone()), ("manual_maintenance", "executing"))
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM maintenance_locks WHERE released_at IS NULL").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT status FROM runs").fetchone()[0], "running")
            self.assertEqual(connection.execute("SELECT action FROM audit_events WHERE action='manual-maintenance-entered'").fetchone()[0], "manual-maintenance-entered")
        from app.modules.maintenance.execution import MAINTENANCE_ADAPTERS

        self.assertFalse(MAINTENANCE_ADAPTERS)

        repeat = self.client.post(
            f"/api/nodes/{node_id}/maintenance-mode/enter",
            headers=headers,
            json={"reason": "same request", "duration_seconds": 60, "idempotency_key": "different"},
        )
        self.assertEqual(repeat.status_code, 201, repeat.text)
        self.assertEqual(repeat.json()["plan_id"], payload["plan_id"])

    def test_exit_requires_fresh_health_and_enters_recovery(self):
        headers = self.login()
        node_id = self.node(headers)
        entered = self.client.post(
            f"/api/nodes/{node_id}/maintenance-mode/enter",
            headers=headers,
            json={"reason": "kernel maintenance", "duration_seconds": 600},
        )
        self.assertEqual(entered.status_code, 201, entered.text)
        self.observe(node_id, observed_at=datetime.now(timezone.utc) - timedelta(minutes=10))
        exited = self.client.post(f"/api/nodes/{node_id}/maintenance-mode/exit", headers=headers, json={})
        self.assertEqual(exited.status_code, 409, exited.text)
        state = self.client.get(f"/api/nodes/{node_id}/maintenance-mode", headers=headers)
        self.assertEqual(state.status_code, 200)
        self.assertEqual(state.json()["state"], "recovery_required")
        with self.main.db() as connection:
            self.assertEqual(connection.execute("SELECT status FROM runs").fetchone()[0], "recovery_required")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM maintenance_locks WHERE released_at IS NULL").fetchone()[0], 1)

    def test_exit_releases_lock_after_fresh_healthy_observation(self):
        headers = self.login()
        node_id = self.node(headers)
        entered = self.client.post(
            f"/api/nodes/{node_id}/maintenance-mode/enter",
            headers=headers,
            json={"reason": "kernel maintenance", "duration_seconds": 600},
        )
        self.assertEqual(entered.status_code, 201, entered.text)
        self.observe(node_id)
        exited = self.client.post(f"/api/nodes/{node_id}/maintenance-mode/exit", headers=headers, json={})
        self.assertEqual(exited.status_code, 200, exited.text)
        self.assertEqual(exited.json()["state"], "available")
        with self.main.db() as connection:
            self.assertEqual(connection.execute("SELECT lifecycle_state FROM maintenance_plans").fetchone()[0], "succeeded")
            self.assertEqual(connection.execute("SELECT status FROM runs").fetchone()[0], "succeeded")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM maintenance_locks WHERE released_at IS NULL").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT action FROM audit_events WHERE action='manual-maintenance-exited'").fetchone()[0], "manual-maintenance-exited")

    def test_planning_does_not_authorize_entry_but_active_mode_can_exit_after_entry_is_disabled(self):
        headers = self.login()
        node_id = self.node(headers)
        self.main.MAINTENANCE_CAPABILITIES.update({
            "planning": True,
            "manual_maintenance_entry": False,
        })
        blocked = self.client.post(
            f"/api/nodes/{node_id}/maintenance-mode/enter",
            headers=headers,
            json={"reason": "kernel maintenance", "duration_seconds": 600},
        )
        self.assertEqual(blocked.status_code, 409, blocked.text)
        self.assertIn("entry", blocked.json()["detail"].lower())

        self.main.MAINTENANCE_CAPABILITIES["manual_maintenance_entry"] = True
        entered = self.client.post(
            f"/api/nodes/{node_id}/maintenance-mode/enter",
            headers=headers,
            json={"reason": "kernel maintenance", "duration_seconds": 600},
        )
        self.assertEqual(entered.status_code, 201, entered.text)
        self.observe(node_id)

        self.main.MAINTENANCE_CAPABILITIES.update({
            "planning": False,
            "manual_maintenance_entry": False,
        })
        exited = self.client.post(
            f"/api/nodes/{node_id}/maintenance-mode/exit",
            headers=headers,
            json={"reason": "maintenance complete"},
        )
        self.assertEqual(exited.status_code, 200, exited.text)
        self.assertEqual(exited.json()["state"], "available")

    def test_exit_with_expired_lock_requires_recovery_without_remote_action(self):
        headers = self.login()
        node_id = self.node(headers)
        entered = self.client.post(
            f"/api/nodes/{node_id}/maintenance-mode/enter",
            headers=headers,
            json={"reason": "kernel maintenance", "duration_seconds": 600},
        )
        self.assertEqual(entered.status_code, 201, entered.text)
        self.observe(node_id)
        with self.main.db() as connection:
            connection.execute(
                "UPDATE maintenance_locks SET expires_at=? WHERE released_at IS NULL",
                ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),),
            )
        exited = self.client.post(f"/api/nodes/{node_id}/maintenance-mode/exit", headers=headers, json={})
        self.assertEqual(exited.status_code, 409, exited.text)
        state = self.client.get(f"/api/nodes/{node_id}/maintenance-mode", headers=headers)
        self.assertEqual(state.json()["state"], "recovery_required")
        with self.main.db() as connection:
            self.assertEqual(connection.execute("SELECT status FROM runs").fetchone()[0], "recovery_required")
