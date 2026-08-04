"""Filebeat companion reconciliation owned by the versions module."""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Awaitable, Callable
from pathlib import Path

import yaml
from fastapi import HTTPException

from app.modules.platform import RunDescriptor


class FilebeatReconcileWorker:
    """Reconcile Filebeat companions and own their runtime observations."""

    def __init__(
        self,
        *,
        db_factory: Callable,
        variables_dir: Path,
        cluster_record: Callable,
        assignment_record: Callable,
        payload: Callable,
        command: Callable,
        stream_command: Callable,
        add_log: Callable[[int, str], None],
        repository_factory: Callable,
        finish_run: Callable,
        active_cluster_operation: Callable | None = None,
        start_run: Callable | None = None,
        inventory_factory: Callable[[int], Path] | None = None,
        audit_event: Callable | None = None,
        run_reconcile: Callable[[int, int, Path], Awaitable[None]] | None = None,
        create_task: Callable = asyncio.create_task,
        run_descriptor: type = RunDescriptor,
    ):
        self._db = db_factory
        self._variables = variables_dir
        self._cluster_record = cluster_record
        self._assignment_record = assignment_record
        self._payload = payload
        self._command = command
        self._stream = stream_command
        self._add_log = add_log
        self._repository = repository_factory
        self._finish_run = finish_run
        self._active_operation = active_cluster_operation
        self._start_run = start_run
        self._inventory = inventory_factory
        self._audit_event = audit_event
        self._run_reconcile = run_reconcile or self.run
        self._create_task = create_task
        self._run_descriptor = run_descriptor

    async def execute(
        self,
        run_id: int,
        inventory_path: Path,
        payload: dict,
        name: str,
        suffix: str,
    ) -> tuple[bool, str]:
        variables_path = self._variables / f"run-{run_id}-filebeat-{suffix}.yaml"
        variables_path.write_text(yaml.safe_dump(payload, sort_keys=True), encoding="utf-8")
        os.chmod(variables_path, 0o600)
        command = self._command(inventory_path, variables_path, name)
        self._add_log(run_id, "$ " + " ".join(command) + "\n")
        output_lines: list[str] = []
        try:
            def record_line(value: str) -> None:
                output_lines.append(value)
                self._add_log(run_id, value)

            succeeded = await asyncio.to_thread(self._stream, command, record_line) == 0
            return succeeded, "".join(output_lines)
        except Exception as error:
            output = "Runner error: " + str(error) + "\n"
            self._add_log(run_id, output)
            return False, output
        finally:
            variables_path.unlink(missing_ok=True)

    def record_observation(self, assignment_id: int, output: str, succeeded: bool) -> None:
        match = re.search(r"ECP_FILEBEAT=(\d+)\|([a-z_]+)", output)
        if match and int(match.group(1)) == assignment_id:
            state = match.group(2)
            error = "" if succeeded or state == "pending" else "Filebeat reconciliation failed"
        else:
            state = "degraded"
            error = "Filebeat reconciliation did not report companion status" if succeeded else "Filebeat reconciliation failed"
        self._repository().record_filebeat_runtime(assignment_id, state=state, error=error)

    async def run(self, run_id: int, cluster_id: int, inventory_path: Path) -> None:
        succeeded = True
        try:
            with self._db() as connection:
                cluster = self._cluster_record(connection, cluster_id)
            assignments = cluster["assignments"]
            if not assignments:
                self._add_log(run_id, "No managed workloads require Filebeat reconciliation.\n")
                return
            for index, assignment in enumerate(assignments):
                with self._db() as connection:
                    row = self._assignment_record(connection, assignment["id"])
                    payload = self._payload(connection, row)
                result, output = await self.execute(
                    run_id, inventory_path, payload, assignment["node_name"], str(index)
                )
                self.record_observation(assignment["id"], output, result)
                if not result:
                    succeeded = False
        except Exception as error:
            succeeded = False
            self._add_log(run_id, "Filebeat reconciliation error: " + str(error) + "\n")
        finally:
            inventory_path.unlink(missing_ok=True)
            with self._db() as connection:
                self._finish_run(connection, run_id, "succeeded" if succeeded else "failed")

    def launch(self, cluster_id: int, username: str) -> int:
        """Create and schedule a tracked Filebeat reconciliation run."""
        if not all((self._active_operation, self._start_run, self._inventory, self._audit_event)):
            raise RuntimeError("Filebeat launch dependencies are not configured")
        with self._db() as connection:
            cluster = self._cluster_record(connection, cluster_id)
            if self._active_operation(connection, cluster["name"]):
                raise HTTPException(409, "Wait for the active cluster operation to finish")
            run = self._start_run(
                connection,
                self._run_descriptor(
                    "filebeat-reconcile",
                    cluster["name"] + ":filebeat-reconcile",
                    {"filebeat_enabled": cluster["log_monitoring"]["filebeat_enabled"]},
                ),
            )
            enabled = cluster["log_monitoring"]["filebeat_enabled"]
        run_id = run.run_id
        inventory_path = self._inventory(run_id)
        self._create_task(self._run_reconcile(run_id, cluster_id, inventory_path))
        self._audit_event(
            username,
            "cluster_filebeat_reconcile",
            str(cluster_id),
            "enabled" if enabled else "disabled",
        )
        return run_id
