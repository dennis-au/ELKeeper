"""Workload-change batch execution and recovery worker."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from collections.abc import Awaitable, Callable

import yaml


class WorkloadChangeWorker:
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
        execute_logged_command: Callable[[int, list[str]], Awaitable[bool]],
        add_log: Callable[[int, str], None],
        workload_sort_key: Callable[[dict], tuple],
        seal_config: Callable[[str], str],
        finish_run: Callable,
        status_in_connection: Callable,
        launch_filebeat_reconcile: Callable[[int, str], int],
        recovery_required_ids: Callable,
        reconcile_runner: Callable[[int, Path, dict, str, str], Awaitable[bool]] | None = None,
    ):
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
        self._reconcile_runner = reconcile_runner

    def batch_plan(self, connection, run_id: int) -> dict:
        row = self._workloads.from_connection(connection).batch(run_id)
        if not row:
            raise RuntimeError("Workload change batch is unavailable")
        return self._open_config(row["plan_encrypted"])

    def record_progress(self, connection, run_id: int, completed: list[dict]) -> None:
        self._workloads.from_connection(connection).record_batch_progress(
            run_id, [item["client_id"] for item in completed]
        )

    async def execute_reconcile(self, run_id: int, inventory_path: Path, payload: dict, name: str, suffix: str) -> bool:
        if self._reconcile_runner is not None:
            return await self._reconcile_runner(run_id, inventory_path, payload, name, suffix)
        variables_path = self._variables / f"run-{run_id}-workload-{suffix}.yaml"
        variables_path.write_text(yaml.safe_dump(payload, sort_keys=True), encoding="utf-8")
        os.chmod(variables_path, 0o600)
        try:
            return await self._execute(run_id, self._reconcile_command(inventory_path, variables_path, name))
        finally:
            variables_path.unlink(missing_ok=True)

    def workload_payload(self, connection, item: dict, plan: dict, desired_state: str = "present") -> dict:
        row = self._assignment_record(connection, item["assignment_id"])
        created_ids = [change["assignment_id"] for change in plan["changes"] if change["kind"] == "create"]
        config_overrides = {
            change["assignment_id"]: change["config"]
            for change in plan["changes"]
            if change["kind"] == "resources"
        }
        if item["kind"] == "resources" and desired_state == "present":
            config_overrides[item["assignment_id"]] = item["config"]
        if item.get("previous_config") and desired_state == "present":
            config_overrides[item["assignment_id"]] = item["previous_config"]
        return self._cluster_payload(
            connection,
            row,
            desired_state,
            batch_assignment_ids=created_ids,
            config_overrides=config_overrides,
        )

    async def rollback(self, run_id: int, inventory_path: Path, plan: dict, completed: list[dict]) -> bool:
        rolled_back = True
        for index, item in enumerate(reversed(completed)):
            try:
                with self._db() as connection:
                    if item["kind"] == "create":
                        payload = self.workload_payload(connection, item, plan, "purge")
                    else:
                        payload = self.workload_payload(
                            connection,
                            {**item, "previous_config": item["previous_config"]},
                            plan,
                        )
                if not await self.execute_reconcile(run_id, inventory_path, payload, item["node_name"], f"rollback-{index}"):
                    rolled_back = False
            except Exception as error:
                self._add_log(run_id, f"Rollback preparation failed for {item['role']} on {item['node_name']}: {error}\n")
                rolled_back = False
        return rolled_back

    def release(self, connection, run_id: int, plan: dict) -> None:
        self._workloads.from_connection(connection).release_batch_in_connection(connection, run_id, plan["changes"])

    async def recover(self, run_id: int) -> None:
        inventory_path = None
        try:
            with self._db() as connection:
                plan = self.batch_plan(connection, run_id)
                completed_ids = self._workloads.from_connection(connection).completed_batch_client_ids_in_connection(connection, run_id)
            completed = [item for item in plan["changes"] if item["client_id"] in completed_ids]
            inventory_path = self._inventory(run_id)
            if not await self.rollback(run_id, inventory_path, plan, completed):
                return
            with self._db() as connection:
                self.release(connection, run_id, plan)
                self._finish_run(
                    connection,
                    run_id,
                    "failed",
                    log_suffix="Interrupted workload batch rolled back after controller restart.\n",
                )
        except Exception as error:
            self._add_log(run_id, "Recovery rollback error: " + str(error) + "\n")
        finally:
            if inventory_path:
                inventory_path.unlink(missing_ok=True)

    async def recover_all(self) -> None:
        with self._db() as connection:
            candidates = self._workloads.from_connection(connection).recovery_batch_run_ids_in_connection(connection)
            run_ids = self._recovery_required_ids(connection, candidates)
        for run_id in run_ids:
            asyncio.create_task(self.recover(run_id))

    async def run(self, run_id: int, inventory_path: Path) -> None:
        succeeded = False
        companion_cluster_id = None
        completed: list[dict] = []
        plan = None
        try:
            with self._db() as connection:
                plan = self.batch_plan(connection, run_id)
            executable = [change for change in plan["changes"] if change["kind"] in {"create", "resources"}]
            executable.sort(key=self._sort_key)
            for index, item in enumerate(executable):
                completed.append(item)
                with self._db() as connection:
                    self.record_progress(connection, run_id, completed)
                with self._db() as connection:
                    payload = self.workload_payload(connection, item, plan)
                if not await self.execute_reconcile(run_id, inventory_path, payload, item["node_name"], str(index)):
                    self._add_log(run_id, f"Batch apply failed for {item['role']} on {item['node_name']}; starting rollback.\n")
                    break
            else:
                with self._db() as connection:
                    workloads = self._workloads.from_connection(connection)
                    workloads.finalize_batch_in_connection(connection, run_id, plan["changes"], self._seal_config)
                    workloads.delete_batch(run_id)
                succeeded = True
                companion_cluster_id = plan["cluster_id"]
                return

            with self._db() as connection:
                self._workloads.from_connection(connection).set_batch_phase(run_id, "rolling_back")
            if not await self.rollback(run_id, inventory_path, plan, completed):
                self._add_log(run_id, "Rollback requires recovery before workload changes can continue.\n")
                with self._db() as connection:
                    self._finish_run(connection, run_id, "recovery_required")
                return
            with self._db() as connection:
                self.release(connection, run_id, plan)
        except Exception as error:
            self._add_log(run_id, "Batch runner error: " + str(error) + "\n")
            with self._db() as connection:
                self._workloads.from_connection(connection).set_batch_phase(run_id, "rolling_back")
                self._finish_run(connection, run_id, "recovery_required")
            return
        finally:
            inventory_path.unlink(missing_ok=True)
            if succeeded:
                with self._db() as connection:
                    self._finish_run(connection, run_id, "succeeded")
            elif plan:
                with self._db() as connection:
                    status = self._status(connection, run_id)
                    if status != "recovery_required":
                        self._finish_run(connection, run_id, "failed")
            if succeeded and companion_cluster_id:
                try:
                    companion_run_id = self._launch_filebeat(companion_cluster_id, "system")
                    self._add_log(run_id, f"Scheduled Filebeat reconciliation run #{companion_run_id}.\n")
                except Exception as error:
                    self._add_log(run_id, f"Filebeat reconciliation was not scheduled: {error}\n")
