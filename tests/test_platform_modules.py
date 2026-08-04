from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import unittest

from app.modules.platform.audit import write_event
from app.modules.platform.db import connect
from app.modules.platform.runs import append_log, completed_run
from app.modules.platform.runs import stream_run_events
from app.modules.platform.security import (
    StoredSecretError,
    digest,
    open_config,
    open_secret,
    seal_config,
    seal_secret,
    valid_password,
)
from app.modules.platform.command_runs import execute_logged_command, run_commands
from app.modules.platform.integration import PlatformRunOperations
from app.modules.platform.runs import (
    RunDescriptor,
    create_run_in_connection,
    set_running_command_in_connection,
)


class PlatformModuleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "control.db"
        with connect(self.database) as connection:
            connection.executescript(
                """
                CREATE TABLE runs (
                  id INTEGER PRIMARY KEY,
                  kind TEXT NOT NULL,
                  target TEXT NOT NULL,
                  status TEXT NOT NULL,
                  command_json TEXT NOT NULL,
                  log TEXT NOT NULL DEFAULT '',
                  finished_at TEXT,
                  context_json TEXT NOT NULL
                );
                CREATE TABLE audit_events (
                  id INTEGER PRIMARY KEY,
                  username TEXT NOT NULL,
                  action TEXT NOT NULL,
                  item_id TEXT NOT NULL,
                  detail TEXT NOT NULL
                );
                """
            )

    def tearDown(self):
        self.temp.cleanup()

    def test_secret_primitives_preserve_legacy_config_and_reject_bad_secrets(self):
        key = "test-key"
        payload = '{"enabled":true}'
        self.assertEqual(open_config(seal_config(payload, key), key), {"enabled": True})
        self.assertEqual(open_config(payload, key), {"enabled": True})
        self.assertEqual(open_secret(seal_secret("value", key), key), "value")
        with self.assertRaises(StoredSecretError):
            open_secret("not-encrypted", key)

    def test_password_hash_verification_is_constant_contract(self):
        stored = digest("correct")
        self.assertTrue(valid_password("correct", stored))
        self.assertFalse(valid_password("incorrect", stored))
        self.assertFalse(valid_password("correct", "invalid"))

    def test_runs_and_audit_use_only_the_platform_database_boundary(self):
        run_id = completed_run(lambda: connect(self.database), "probe", "node:1", "finished", {"safe": True})
        append_log(lambda: connect(self.database), run_id, "tail\n")
        write_event(lambda: connect(self.database), "admin", "tested", "x" * 300, "y" * 600)
        with connect(self.database) as connection:
            run = connection.execute("SELECT status,log,context_json FROM runs WHERE id=?", (run_id,)).fetchone()
            event = connection.execute("SELECT item_id,detail FROM audit_events").fetchone()
        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(run["log"], "finished\ntail\n")
        self.assertEqual(run["context_json"], '{"safe": true}')
        self.assertEqual(len(event["item_id"]), 256)
        self.assertEqual(len(event["detail"]), 512)

    def test_run_event_stream_uses_platform_database_boundary(self):
        run_id = completed_run(lambda: connect(self.database), "stream", "node:1", "finished")

        async def collect():
            return [item async for item in stream_run_events(lambda: connect(self.database), run_id, poll_seconds=0)]

        import asyncio
        events = asyncio.run(collect())
        self.assertIn("event: log", events[0])
        self.assertIn('"status": "succeeded"', events[1])

    def test_command_run_helpers_preserve_output_and_cleanup(self):
        async def exercise():
            logs = []
            finished = []
            seen = []
            with tempfile.TemporaryDirectory() as directory:
                temporary = Path(directory) / "temporary"
                temporary.write_text("temporary", encoding="utf-8")

                def stream(command, on_line):
                    seen.append(tuple(command))
                    on_line("ok\n")
                    return 0

                success = await execute_logged_command(
                    1, ["true"], add_log=lambda _id, value: logs.append(value), stream_command=stream
                )
                await run_commands(
                    2,
                    [(["true"], {"name": "test"})],
                    result_handler=lambda metadata, output, succeeded: seen.append((metadata, output, succeeded)),
                    temporary_paths=[temporary],
                    add_log=lambda _id, value: logs.append(value),
                    stream_command=stream,
                    finish_run=lambda run_id, status: finished.append((run_id, status)),
                )
                return success, finished, temporary, seen

        success, finished, temporary, seen = asyncio.run(exercise())
        self.assertTrue(success)
        self.assertEqual(finished, [(2, "succeeded")])
        self.assertFalse(temporary.exists())
        self.assertIn(({"name": "test"}, "ok\n", True), seen)

    def test_platform_run_operations_creates_redacted_runs_and_secure_variables(self):
        scheduled = []
        seen = {}

        async def execute(_run_id, _command, _temporary_paths):
            return None

        def schedule(coroutine):
            scheduled.append(coroutine)

        with tempfile.TemporaryDirectory() as directory:
            variables_dir = Path(directory)
            inventory_path = variables_dir / "inventory.ini"
            inventory_path.write_text("[all]\nnode-a\n", encoding="utf-8")
            operations = PlatformRunOperations(
                db_factory=lambda: connect(self.database),
                variables_dir=variables_dir,
                inventory_factory=lambda run_id, **kwargs: seen.setdefault("inventory", inventory_path),
                run_descriptor=RunDescriptor,
                create_run=create_run_in_connection,
                start_run=create_run_in_connection,
                set_running_command=set_running_command_in_connection,
                finish_run=lambda connection, run_id, status: None,
                redacted_command=lambda command: ["redacted", command[0]],
                lifecycle_service=lambda: None,
                run_execute=execute,
                add_log=lambda _run_id, _value: None,
                stream_command=lambda _command, _callback: 0,
                schedule=schedule,
            )
            run_id = operations.launch(
                "apply",
                "cluster-a",
                lambda inventory, variables: seen.update(
                    {"factory_inventory": inventory, "variables": variables}
                ) or ["ansible-playbook", "--password=secret"],
                variables={"safe": True},
                inventory_nodes=[7],
            )
            variables_path = seen["variables"]
            self.assertEqual(seen["factory_inventory"], inventory_path)
            self.assertEqual(variables_path.read_text(encoding="utf-8"), "safe: true\n")
            self.assertEqual(variables_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(len(scheduled), 1)
            scheduled.pop().close()
            with connect(self.database) as connection:
                stored = connection.execute(
                    "SELECT command_json FROM runs WHERE id=?", (run_id,)
                ).fetchone()["command_json"]
            self.assertIn("redacted", stored)
        self.assertNotIn("secret", stored)
