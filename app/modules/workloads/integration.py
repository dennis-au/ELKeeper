"""Public workload batch-operation facade for application assembly.

The workload module owns worker construction, batch lifecycle calls, and run
creation.  Validation and orchestration details are supplied as callbacks so
legacy callers remain patchable while the module boundary stays one-way.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Callable

from fastapi import HTTPException

from app.modules.platform import RunDescriptor

from .worker import WorkloadChangeWorker


class WorkloadOperations:
    """Own workload batch composition without importing application assembly."""

    def __init__(
        self,
        *,
        db_factory: Callable,
        variables_dir: Path,
        inventory_factory: Callable[[int], Path],
        workload_repository: type,
        assignment_record: Callable,
        cluster_payload: Callable,
        open_config: Callable,
        reconcile_command: Callable,
        execute_logged_command: Callable,
        add_log: Callable,
        workload_sort_key: Callable,
        seal_config: Callable,
        finish_run: Callable,
        status_in_connection: Callable,
        launch_filebeat_reconcile: Callable,
        recovery_required_ids: Callable,
        start_run: Callable,
        run_descriptor: type = RunDescriptor,
        validate_change_set: Callable | None = None,
        schedule: Callable = asyncio.create_task,
    ) -> None:
        self._db = db_factory
        self._variables = variables_dir
        self._inventory = inventory_factory
        self._workloads = workload_repository
        self._assignment_record = assignment_record
        self._cluster_payload = cluster_payload
        self._open_config = open_config
        self._reconcile_command = reconcile_command
        self._execute = execute_logged_command
        self._add_log = add_log
        self._sort_key = workload_sort_key
        self._seal_config = seal_config
        self._finish_run = finish_run
        self._status = status_in_connection
        self._launch_filebeat = launch_filebeat_reconcile
        self._recovery_required_ids = recovery_required_ids
        self._start_run = start_run
        self._run_descriptor = run_descriptor
        self._validate_change_set = validate_change_set
        self._schedule = schedule

    def _worker(self, *, reconcile_runner: Callable | None = None) -> WorkloadChangeWorker:
        return WorkloadChangeWorker(
            db_factory=self._db,
            variables_dir=self._variables,
            inventory_factory=self._inventory,
            workload_repository=self._workloads,
            assignment_record=self._assignment_record,
            cluster_payload=self._cluster_payload,
            open_config=self._open_config,
            reconcile_command=self._reconcile_command,
            execute_logged_command=self._execute,
            add_log=self._add_log,
            workload_sort_key=self._sort_key,
            seal_config=self._seal_config,
            finish_run=self._finish_run,
            status_in_connection=self._status,
            launch_filebeat_reconcile=self._launch_filebeat,
            recovery_required_ids=self._recovery_required_ids,
            reconcile_runner=reconcile_runner,
        )

    def batch_plan(self, connection: Any, run_id: int) -> dict:
        return self._worker().batch_plan(connection, run_id)

    def record_progress(self, connection: Any, run_id: int, completed: list[dict]) -> None:
        self._worker().record_progress(connection, run_id, completed)

    async def execute_reconcile(self, run_id: int, inventory: Path, payload: dict, name: str, suffix: str) -> bool:
        return await self._worker().execute_reconcile(run_id, inventory, payload, name, suffix)

    def workload_payload(self, connection: Any, item: dict, plan: dict, desired_state: str = "present") -> dict:
        return self._worker().workload_payload(connection, item, plan, desired_state)

    async def rollback(self, run_id: int, inventory: Path, plan: dict, completed: list[dict]) -> bool:
        return await self._worker().rollback(run_id, inventory, plan, completed)

    def release(self, connection: Any, run_id: int, plan: dict) -> None:
        self._worker().release(connection, run_id, plan)

    async def recover(self, run_id: int) -> None:
        await self._worker().recover(run_id)

    async def recover_all(self) -> None:
        await self._worker().recover_all()

    async def run_batch(
        self,
        run_id: int,
        inventory: Path,
        *,
        reconcile_runner: Callable | None = None,
    ) -> None:
        await self._worker(reconcile_runner=reconcile_runner or self.execute_reconcile).run(run_id, inventory)

    def launch_batch(self, cluster_id: int, input: Any) -> int:
        if self._validate_change_set is None:
            raise RuntimeError("Workload validation dependency is not configured")
        with self._db() as connection:
            cluster, plan_changes = self._validate_change_set(connection, cluster_id, input)
            run = self._start_run(
                connection,
                self._run_descriptor(
                    kind="workload-apply",
                    target=cluster["name"] + ":workload-apply",
                    context={
                        "change_count": len(plan_changes),
                        "kinds": [item["kind"] for item in plan_changes],
                    },
                ),
            )
            run_id = run.run_id
            try:
                self._workloads.from_connection(connection).stage_batch_changes_in_connection(
                    connection, cluster_id, run_id, plan_changes, self._seal_config
                )
            except RuntimeError as error:
                if str(error) == "workload_revision_conflict":
                    raise HTTPException(
                        409,
                        "This workload changed since it was staged; refresh and stage it again",
                    ) from error
                raise
            plan = {"cluster_id": cluster_id, "changes": plan_changes}
            self._workloads.from_connection(connection).create_batch_in_connection(
                connection,
                run_id=run_id,
                cluster_id=cluster_id,
                plan_encrypted=self._seal_config(json.dumps(plan)),
            )
        inventory = self._inventory(run_id)
        self._schedule(self.run_batch(run_id, inventory))
        return run_id


__all__ = ["WorkloadOperations"]
