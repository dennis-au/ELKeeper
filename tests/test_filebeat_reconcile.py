from __future__ import annotations

import asyncio
from contextlib import contextmanager
from pathlib import Path
import tempfile
import unittest

from app.modules.versions.filebeat import FilebeatReconcileWorker


@contextmanager
def _db():
    yield object()


class _RuntimeRepository:
    def __init__(self):
        self.observations = []

    def record_filebeat_runtime(self, assignment_id, *, state, error):
        self.observations.append((assignment_id, state, error))


class FilebeatSelectedReconcileTests(unittest.IsolatedAsyncioTestCase):
    async def test_selected_assignment_run_does_not_reconcile_peer_workloads(self):
        repository = _RuntimeRepository()
        finished = []
        worker = FilebeatReconcileWorker(
            db_factory=_db,
            variables_dir=Path(tempfile.gettempdir()),
            cluster_record=lambda _connection, _cluster_id: {
                "assignments": [
                    {"id": 11, "node_name": "node-a"},
                    {"id": 12, "node_name": "node-b"},
                ],
            },
            assignment_record=lambda _connection, assignment_id: {"id": assignment_id},
            payload=lambda _connection, record: record,
            command=lambda *_args: (),
            stream_command=lambda *_args: 0,
            add_log=lambda *_args: None,
            repository_factory=lambda: repository,
            finish_run=lambda _connection, run_id, status: finished.append((run_id, status)),
        )
        executed = []

        async def execute(run_id, inventory_path, payload, name, suffix):
            executed.append((run_id, payload["id"], name, suffix))
            return True, "ECP_FILEBEAT=" + str(payload["id"]) + "|running\n"

        worker.execute = execute
        with tempfile.TemporaryDirectory() as temporary:
            inventory = Path(temporary) / "inventory.yaml"
            inventory.write_text("all: {}", encoding="ascii")
            await worker.run(77, 1, inventory, assignment_ids=(11,))

        self.assertEqual(executed, [(77, 11, "node-a", "0")])
        self.assertEqual(repository.observations, [(11, "running", "")])
        self.assertEqual(finished, [(77, "succeeded")])

    async def test_selected_assignment_run_rejects_unknown_assignment_before_any_remote_execution(self):
        worker = FilebeatReconcileWorker(
            db_factory=_db,
            variables_dir=Path(tempfile.gettempdir()),
            cluster_record=lambda _connection, _cluster_id: {"assignments": [{"id": 11, "node_name": "node-a"}]},
            assignment_record=lambda _connection, assignment_id: {"id": assignment_id},
            payload=lambda _connection, record: record,
            command=lambda *_args: (),
            stream_command=lambda *_args: 0,
            add_log=lambda *_args: None,
            repository_factory=_RuntimeRepository,
            finish_run=lambda *_args: None,
        )
        executed = []

        async def execute(*args):
            executed.append(args)
            return True, ""

        worker.execute = execute
        with tempfile.TemporaryDirectory() as temporary:
            inventory = Path(temporary) / "inventory.yaml"
            inventory.write_text("all: {}", encoding="ascii")
            await worker.run(77, 1, inventory, assignment_ids=(12,))

        self.assertEqual(executed, [])
