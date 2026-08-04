"""Guarded upgrade worker with injected persistence and orchestration seams."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Awaitable, Callable

import yaml


class VersionUpgradeWorker:
    def __init__(
        self,
        *,
        db_factory: Callable,
        variables_dir: Path,
        assignment_record: Callable,
        cluster_record: Callable,
        cluster_payload: Callable,
        reconcile_command: Callable,
        upgrade_preflight_command: Callable,
        execute_logged_command: Callable[[int, list[str]], Awaitable[bool]],
        add_log: Callable[[int, str], None],
        platform_finish_run: Callable,
        workload_repository: Callable,
        version_key: Callable[[str | None], tuple[int, int, int] | None],
        launch_filebeat_reconcile: Callable[[int, str], int],
    ):
        self._db = db_factory
        self._variables = variables_dir
        self._assignment_record = assignment_record
        self._cluster_record = cluster_record
        self._cluster_payload = cluster_payload
        self._reconcile_command = reconcile_command
        self._upgrade_preflight_command = upgrade_preflight_command
        self._execute = execute_logged_command
        self._add_log = add_log
        self._finish_run = platform_finish_run
        self._workloads = workload_repository
        self._version_key = version_key
        self._launch_filebeat = launch_filebeat_reconcile

    async def run(self, run_id: int, cluster_id: int, target_version: str, inventory_path: Path, assignment_ids: list[int]) -> None:
        paths = [inventory_path]
        succeeded = False
        try:
            with self._db() as connection:
                first = self._assignment_record(connection, assignment_ids[0])
                preflight_payload = self._cluster_payload(connection, first)
                cluster = self._cluster_record(connection, cluster_id)
                observed_versions = [
                    item["observation"]["version"]
                    for item in cluster["assignments"]
                    if item["observation"]
                ]
                preflight_payload["target_version"] = target_version
                preflight_payload["upgrade_major"] = any(
                    self._version_key(target_version)[0] > self._version_key(value)[0]
                    for value in observed_versions
                    if self._version_key(value)
                )
            preflight_vars = self._variables / f"run-{run_id}-preflight.yaml"
            preflight_vars.write_text(yaml.safe_dump(preflight_payload, sort_keys=True), encoding="utf-8")
            os.chmod(preflight_vars, 0o600)
            paths.append(preflight_vars)
            if not await self._execute(
                run_id,
                self._upgrade_preflight_command(inventory_path, preflight_vars, first["node_name"]),
            ):
                return
            for index, assignment_id in enumerate(assignment_ids):
                with self._db() as connection:
                    row = self._assignment_record(connection, assignment_id)
                    cluster = self._cluster_record(connection, cluster_id)
                    assignment = next(item for item in cluster["assignments"] if item["id"] == assignment_id)
                    previous_version = assignment["observation"]["version"]
                    payload = self._cluster_payload(connection, row)
                    payload["assignment"]["image_version"] = target_version
                variables_path = self._variables / f"run-{run_id}-upgrade-{index}.yaml"
                variables_path.write_text(yaml.safe_dump(payload, sort_keys=True), encoding="utf-8")
                os.chmod(variables_path, 0o600)
                paths.append(variables_path)
                if await self._execute(run_id, self._reconcile_command(inventory_path, variables_path, row["node_name"])):
                    with self._db() as connection:
                        self._workloads.from_connection(connection).set_image_version_in_connection(
                            connection, assignment_id, target_version
                        )
                    continue
                self._add_log(
                    run_id,
                    f"Upgrade failed for {row['role']} on {row['node_name']}; restoring {previous_version}.\n",
                )
                rollback_payload = dict(payload)
                rollback_payload["assignment"] = dict(payload["assignment"])
                rollback_payload["assignment"]["image_version"] = previous_version
                rollback_path = self._variables / f"run-{run_id}-rollback-{index}.yaml"
                rollback_path.write_text(yaml.safe_dump(rollback_payload, sort_keys=True), encoding="utf-8")
                os.chmod(rollback_path, 0o600)
                paths.append(rollback_path)
                await self._execute(run_id, self._reconcile_command(inventory_path, rollback_path, row["node_name"]))
                return
            succeeded = True
        finally:
            for path in paths:
                Path(path).unlink(missing_ok=True)
            with self._db() as connection:
                self._finish_run(connection, run_id, "succeeded" if succeeded else "failed")
            if succeeded:
                try:
                    companion_run_id = self._launch_filebeat(cluster_id, "system")
                    self._add_log(run_id, f"Scheduled Filebeat reconciliation run #{companion_run_id}.\n")
                except Exception as error:
                    self._add_log(run_id, f"Filebeat reconciliation was not scheduled: {error}\n")
