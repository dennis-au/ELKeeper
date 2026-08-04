from __future__ import annotations

import base64
import hashlib
import hmac
import os
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timezone

from app.modules.platform.auth import (
    run_events_token,
    signed_scope_token,
    signed_token,
    token_user,
    valid_run_events_token,
    valid_scope_token,
)
from app.modules.platform.security import redact_config
from app.modules.platform.auth import password_matches
from app.modules.platform.db import control_db
from app.modules.platform.maintenance import MAINTENANCE_CAPABILITIES, capability_snapshot
from app.modules.platform.runs import (
    RunContext,
    RunDescriptor,
    RunEvent,
    RunEventType,
    RunState,
    append_event,
    append_log_in_connection,
    complete_run,
    context_and_log_in_connection,
    finish_run_in_connection,
    create_run,
    mark_recovery_required_in_connection,
    rename_target_in_connection,
    set_running_command_in_connection,
    statuses_in_connection,
    transition_run,
    start_run_in_connection,
)


def _token(username: str, issued: int, key: str) -> str:
    payload = f"{username}:{issued}".encode()
    signature = hmac.new(key.encode(), payload, hashlib.sha256).digest()
    encode = lambda value: base64.urlsafe_b64encode(value).decode().rstrip("=")
    return f"{encode(payload)}.{encode(signature)}"


class PlatformContractTests(unittest.TestCase):
    def test_control_db_uses_configured_data_boundary_and_commits(self):
        with tempfile.TemporaryDirectory() as temporary:
            previous = os.environ.get("APP_DATA_DIR")
            os.environ["APP_DATA_DIR"] = temporary
            try:
                with control_db() as connection:
                    connection.execute("CREATE TABLE contract_probe(value TEXT NOT NULL)")
                    connection.execute("INSERT INTO contract_probe(value) VALUES ('ok')")
                with control_db() as connection:
                    row = connection.execute("SELECT value FROM contract_probe").fetchone()
                self.assertIsInstance(row, sqlite3.Row)
                self.assertEqual(row["value"], "ok")
            finally:
                if previous is None:
                    os.environ.pop("APP_DATA_DIR", None)
                else:
                    os.environ["APP_DATA_DIR"] = previous

    def test_token_user_validates_signature_and_expiry(self):
        key = "contract-test-key"
        current = int(time.time())
        self.assertEqual(token_user(_token("operator", current, key), key=key), "operator")
        self.assertIsNone(token_user(_token("operator", current, "wrong"), key=key))
        self.assertIsNone(token_user(_token("operator", current - 28801, key), key=key))
        self.assertIsNone(token_user("not-a-token", key=key))

    def test_platform_issues_and_validates_browser_run_and_scope_tokens(self):
        key = "contract-test-key"
        issued = 1_700_000_000
        browser = signed_token("operator", key=key, now=issued)
        run = run_events_token(42, key=key, now=issued)
        scope = signed_scope_token("dashboard", key=key, now=issued)
        self.assertEqual(token_user(browser, key=key, now=issued + 1), "operator")
        self.assertTrue(valid_run_events_token(run, 42, key=key, now=issued + 1))
        self.assertFalse(valid_run_events_token(run, 43, key=key, now=issued + 1))
        self.assertTrue(valid_scope_token(scope, "dashboard", key=key, now=issued + 1))
        self.assertFalse(valid_scope_token(scope, "runs", key=key, now=issued + 1))
        self.assertFalse(valid_scope_token(scope, "dashboard", key=key, now=issued + 601))

    def test_config_redaction_preserves_configured_state_without_secret_values(self):
        result = redact_config(
            {"password": "hidden", "nested": {"api_key": "hidden"}, "enabled": True}
        )
        self.assertEqual(result, {"password": "configured", "nested": {"api_key": "configured"}, "enabled": True})

    def test_capability_snapshot_is_a_copy(self):
        snapshot = capability_snapshot()
        self.assertEqual(set(snapshot), set(MAINTENANCE_CAPABILITIES))
        snapshot["planning"] = not snapshot["planning"]
        self.assertNotEqual(snapshot["planning"], MAINTENANCE_CAPABILITIES["planning"])

    def test_password_check_is_owned_by_platform_auth(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = os.path.join(temporary, "users.db")

            def factory():
                connection = sqlite3.connect(path)
                connection.row_factory = sqlite3.Row
                return connection

            with factory() as connection:
                connection.execute("CREATE TABLE users(username TEXT PRIMARY KEY,password_hash TEXT NOT NULL)")
                connection.execute("INSERT INTO users VALUES ('operator','stored')")
            self.assertTrue(password_matches(factory, "operator", "correct", lambda value, stored: value == "correct" and stored == "stored"))
            self.assertFalse(password_matches(factory, "operator", "wrong", lambda value, stored: value == "correct" and stored == "stored"))

    def test_run_contracts_redact_context_and_metadata(self):
        descriptor = RunDescriptor("upgrade", "cluster-a", {"username": "operator", "password": "hidden"})
        self.assertEqual(descriptor.context["password"], "[REDACTED]")
        context = RunContext(7, descriptor)
        event = RunEvent(7, RunEventType.PROGRESS, "working", {"token": "hidden", "step": 2})
        self.assertEqual(context.state, RunState.QUEUED)
        self.assertEqual(event.metadata["token"], "[REDACTED]")

    def test_run_contracts_reject_invalid_identity_and_naive_timestamp(self):
        with self.assertRaises(ValueError):
            RunDescriptor("", "cluster-a")
        with self.assertRaises(ValueError):
            RunContext(0, RunDescriptor("apply", "cluster-a"))
        with self.assertRaises(ValueError):
            RunEvent(1, RunEventType.OUTPUT, occurred_at=datetime.now())

    def test_run_persistence_contracts_use_existing_runs_table(self):
        with tempfile.TemporaryDirectory() as temporary:
            db_path = os.path.join(temporary, "runs.db")

            def factory():
                connection = sqlite3.connect(db_path)
                connection.row_factory = sqlite3.Row
                return connection

            with factory() as connection:
                connection.execute(
                    "CREATE TABLE runs (id INTEGER PRIMARY KEY, kind TEXT, target TEXT, status TEXT, command_json TEXT, log TEXT DEFAULT '', finished_at TEXT, context_json TEXT)"
                )
            descriptor = RunDescriptor("probe", "node-a", {"token": "secret"})
            context = create_run(factory, descriptor, ["echo", "ok"])
            self.assertEqual(context.state, RunState.QUEUED)
            transition_run(factory, context.run_id, RunState.RUNNING)
            append_event(factory, RunEvent(context.run_id, RunEventType.OUTPUT, "ready", {"password": "hidden"}))
            complete_run(
                factory,
                RunEvent(
                    context.run_id,
                    RunEventType.COMPLETED,
                    "done",
                    occurred_at=datetime.now(timezone.utc),
                ),
            )
            with factory() as connection:
                row = connection.execute("SELECT * FROM runs WHERE id=?", (context.run_id,)).fetchone()
            self.assertEqual(row["status"], "succeeded")
            self.assertIn("[completed] done", row["log"])
            self.assertNotIn("secret", row["context_json"])

    def test_recovery_transition_preserves_closed_runs_and_redacts_only_platform_write(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(
            "CREATE TABLE runs (id INTEGER PRIMARY KEY, status TEXT, log TEXT DEFAULT '', finished_at TEXT)"
        )
        connection.execute("INSERT INTO runs(id,status,finished_at) VALUES (1,'running',NULL),(2,'succeeded','old')")
        mark_recovery_required_in_connection(connection, [1, 2], "controller restart")
        rows = connection.execute("SELECT id,status,log,finished_at FROM runs ORDER BY id").fetchall()
        self.assertEqual(rows[0]["status"], "recovery_required")
        self.assertIn("controller restart", rows[0]["log"])
        self.assertEqual(rows[1]["status"], "succeeded")
        self.assertEqual(rows[1]["finished_at"], "old")

    def test_start_and_finish_run_contracts_preserve_terminal_timestamp(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(
            "CREATE TABLE runs (id INTEGER PRIMARY KEY, kind TEXT, target TEXT, status TEXT, command_json TEXT, log TEXT DEFAULT '', finished_at TEXT, context_json TEXT)"
        )
        context = start_run_in_connection(connection, RunDescriptor("apply", "cluster-a"))
        self.assertEqual(context.state, RunState.RUNNING)
        finish_run_in_connection(connection, context.run_id, "failed", log_suffix="failed\n")
        row = connection.execute("SELECT status,finished_at,log FROM runs WHERE id=?", (context.run_id,)).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertIsNotNone(row["finished_at"])
        self.assertIn("failed", row["log"])

    def test_worker_helpers_own_legacy_context_command_and_target_writes(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(
            "CREATE TABLE runs (id INTEGER PRIMARY KEY, kind TEXT, target TEXT, status TEXT, command_json TEXT, log TEXT DEFAULT '', finished_at TEXT, context_json TEXT)"
        )
        context = start_run_in_connection(
            connection,
            RunDescriptor("enroll", "pending-1", {"enrollment_node_id": 7}),
        )
        set_running_command_in_connection(connection, context.run_id, ["ansible-playbook", "host.yml"])
        append_log_in_connection(connection, context.run_id, "ECP_HOSTNAME=node-a\n")
        rename_target_in_connection(connection, context.run_id, "node-a")
        stored_context, log = context_and_log_in_connection(connection, context.run_id)
        row = connection.execute("SELECT target,status,command_json FROM runs WHERE id=?", (context.run_id,)).fetchone()
        self.assertEqual(stored_context, {"enrollment_node_id": 7})
        self.assertEqual(log, "ECP_HOSTNAME=node-a\n")
        self.assertEqual((row["target"], row["status"]), ("node-a", "running"))
        self.assertEqual(row["command_json"], '["ansible-playbook", "host.yml"]')

    def test_platform_run_status_projection_handles_empty_and_missing_references(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY, status TEXT)")
        connection.execute("INSERT INTO runs(id,status) VALUES (1,'running'),(2,'succeeded')")
        self.assertEqual(statuses_in_connection(connection, []), {})
        self.assertEqual(statuses_in_connection(connection, [2, 1, 999]), {1: "running", 2: "succeeded"})


if __name__ == "__main__":
    unittest.main()
