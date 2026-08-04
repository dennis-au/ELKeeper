"""Cluster zoning reconciliation orchestration.

The zoning worker owns the sequencing and persistence policy for allocation
awareness.  Concrete database, payload, Ansible, and run services are injected
so the compatibility facade in :mod:`app.main` remains patchable while routes
move to the clusters boundary.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from collections.abc import Awaitable, Callable

import yaml
from fastapi import HTTPException
from app.modules.platform import RunDescriptor


class ZoningWorker:
    def __init__(
        self,
        *,
        db_factory: Callable,
        variables_dir: Path,
        cluster_record: Callable,
        assignment_record: Callable,
        cluster_payload: Callable,
        cluster_repository: type,
        workload_repository: type,
        host_repository: type,
        elasticsearch_roles: tuple[str, ...],
        zoning_reconcile_order: tuple[str, ...],
        active_cluster_operation: Callable,
        require_cluster_host_zone: Callable,
        reconcile_command: Callable,
        zoning_settings_command: Callable,
        execute_logged_command: Callable[[int, list[str]], Awaitable[bool]],
        add_log: Callable[[int, str], None],
        append_log: Callable,
        finish_run: Callable,
        start_run: Callable,
        completed_run: Callable,
        open_config: Callable,
        inventory_factory: Callable[[int], Path] | None = None,
        reconcile_runner: Callable | None = None,
        settings_runner: Callable | None = None,
        role_specs: dict | None = None,
        run_descriptor: type = RunDescriptor,
    ):
        self._db = db_factory
        self._variables = variables_dir
        self._cluster_record = cluster_record
        self._assignment_record = assignment_record
        self._cluster_payload = cluster_payload
        self._clusters = cluster_repository
        self._workloads = workload_repository
        self._hosts = host_repository
        self._role_specs = role_specs or {
            "hot": {"label": "Hot data"},
            "warm": {"label": "Warm data"},
        }
        self._es_roles = tuple(elasticsearch_roles)
        self._order = tuple(zoning_reconcile_order)
        self._active_operation = active_cluster_operation
        self._require_zone = require_cluster_host_zone
        self._reconcile_command = reconcile_command
        self._settings_command = zoning_settings_command
        self._execute = execute_logged_command
        self._add_log = add_log
        self._append_log = append_log
        self._finish = finish_run
        self._start = start_run
        self._descriptor = run_descriptor
        self._completed_run = completed_run
        self._open_config = open_config
        self._inventory = inventory_factory
        self._reconcile_runner = reconcile_runner
        self._settings_runner = settings_runner

    def assignments(self, cluster: dict) -> list[dict]:
        return sorted(
            [assignment for assignment in cluster["assignments"] if assignment["role"] in self._es_roles],
            key=lambda assignment: (
                self._order.index(assignment["role"]),
                assignment["node_name"],
                assignment["id"],
            ),
        )

    def preflight(self, connection, cluster_id: int) -> tuple[dict, list[dict]]:
        cluster = self._cluster_record(connection, cluster_id)
        if self._active_operation(connection, cluster["name"]):
            raise HTTPException(409, "Wait for the active cluster operation to finish")
        zoning = cluster["zoning"]
        assignments = self.assignments(cluster)
        if zoning["mode"] == "disabled":
            if assignments and not any(item["role"] == "master" for item in assignments):
                raise HTTPException(422, "Deploy a master or purge Elasticsearch workloads before disabling zoning")
            return cluster, assignments
        if len(zoning["zones"]) < 2:
            raise HTTPException(422, "Allocation awareness requires at least two defined zones")
        if not any(item["role"] == "master" for item in assignments):
            raise HTTPException(422, "Deploy a master before applying cluster zoning")
        data_assignments = [item for item in assignments if item["role"] in {"hot", "warm"}]
        if not data_assignments:
            raise HTTPException(422, "Deploy hot or warm data workloads before applying cluster zoning")
        members = {member["node_id"]: member for member in cluster["members"]}
        for assignment in assignments:
            self._require_zone(cluster, members.get(assignment["node_id"]))
        data_zones = {members[item["node_id"]]["zone_id"] for item in data_assignments}
        if zoning["mode"] == "forced_awareness":
            for role in ("hot", "warm"):
                role_assignments = [item for item in data_assignments if item["role"] == role]
                if not role_assignments:
                    continue
                role_zones = {members[item["node_id"]]["zone_id"] for item in role_assignments}
                missing = [zone for zone in zoning["zones"] if zone not in role_zones]
                if missing:
                    raise HTTPException(
                        422,
                        f"{self._role_specs[role]['label']} does not cover forced zones: {', '.join(missing)}",
                    )
        if len(data_zones) < 2:
            raise HTTPException(422, "Data workloads must span at least two defined zones")
        return cluster, assignments

    async def execute_reconcile(self, run_id: int, inventory_path: Path, payload: dict, name: str, suffix: str) -> bool:
        variables_path = self._variables / f"run-{run_id}-zoning-{suffix}.yaml"
        variables_path.write_text(yaml.safe_dump(payload, sort_keys=True), encoding="utf-8")
        os.chmod(variables_path, 0o600)
        try:
            return await self._execute(run_id, self._reconcile_command(inventory_path, variables_path, name))
        finally:
            variables_path.unlink(missing_ok=True)

    async def execute_settings(self, run_id: int, inventory_path: Path, payload: dict, name: str) -> bool:
        variables_path = self._variables / f"run-{run_id}-zoning-settings.yaml"
        variables_path.write_text(yaml.safe_dump(payload, sort_keys=True), encoding="utf-8")
        os.chmod(variables_path, 0o600)
        try:
            return await self._execute(run_id, self._settings_command(inventory_path, variables_path, name))
        finally:
            variables_path.unlink(missing_ok=True)

    def settings_payload(self, connection, cluster: dict) -> tuple[dict, str]:
        master = next((assignment for assignment in cluster["assignments"] if assignment["role"] == "master"), None)
        if not master:
            raise HTTPException(422, "Deploy a master before applying cluster zoning")
        member = next(member for member in cluster["members"] if member["node_id"] == master["node_id"])
        credentials = self._open_config(self._clusters.from_connection(connection).secrets_json(cluster["id"]))
        return {
            "cluster": {"id": cluster["id"], "name": cluster["name"], "slug": cluster["slug"]},
            "bootstrap": {
                "node_name": master["node_name"],
                "node_id": master["node_id"],
                "user_address": member["user_address"],
                "ports": cluster["role_ports"]["master"],
            },
            "credentials": credentials,
            "zoning": cluster["zoning"],
        }, master["node_name"]

    async def rollback(self, run_id: int, inventory_path: Path, completed: list[tuple[dict, dict]], previous_zones: dict, *, reconcile=None) -> bool:
        reconcile = reconcile or self._reconcile_runner or self.execute_reconcile
        succeeded = True
        for index, (assignment, payload) in enumerate(reversed(completed)):
            rollback_payload = json.loads(json.dumps(payload))
            rollback_payload["membership"]["zone_id"] = previous_zones.get(str(assignment["id"])) or ""
            if not await reconcile(run_id, inventory_path, rollback_payload, assignment["node_name"], f"rollback-{index}"):
                succeeded = False
        return succeeded

    async def run_apply(self, run_id: int, cluster_id: int, inventory_path: Path, *, reconcile=None, settings=None) -> None:
        # ``reconcile`` and ``settings`` are compatibility seams supplied by
        # app.main. Tests patch those names before invoking this worker.
        reconcile = reconcile or self._reconcile_runner or self.execute_reconcile
        settings = settings or self._settings_runner or self.execute_settings
        succeeded = False
        completed: list[tuple[dict, dict]] = []
        previous_zones: dict = {}
        error_message = "Zoning application failed"
        try:
            with self._db() as connection:
                cluster = self._cluster_record(connection, cluster_id)
                assignments = self.assignments(cluster)
                observation = self._clusters.from_connection(connection).zoning_observation_record_in_connection(connection, cluster_id)
                if observation:
                    previous_zones = json.loads(observation["observed_zones_json"] or "{}")
                desired_zones = {
                    str(assignment["id"]): (
                        next(member["zone_id"] for member in cluster["members"] if member["node_id"] == assignment["node_id"])
                        if cluster["zoning"]["mode"] != "disabled" else ""
                    )
                    for assignment in assignments
                }
            for index, assignment in enumerate(assignments):
                if previous_zones.get(str(assignment["id"]), "") == desired_zones[str(assignment["id"] )]:
                    continue
                with self._db() as connection:
                    payload = self._cluster_payload(connection, self._assignment_record(connection, assignment["id"]))
                payload["membership"]["zone_id"] = desired_zones[str(assignment["id"])]
                if not await reconcile(run_id, inventory_path, payload, assignment["node_name"], str(index)):
                    error_message = f"Zoning reconciliation failed for {assignment['role']} on {assignment['node_name']}"
                    if not await self._rollback_with(run_id, inventory_path, completed, previous_zones, reconcile):
                        error_message += "; rollback was incomplete"
                    return
                completed.append((assignment, payload))
            with self._db() as connection:
                cluster = self._cluster_record(connection, cluster_id)
                settings_payload, master_name = self.settings_payload(connection, cluster)
            if not await settings(run_id, inventory_path, settings_payload, master_name):
                error_message = "Elasticsearch rejected the zoning settings"
                if not await self._rollback_with(run_id, inventory_path, completed, previous_zones, reconcile):
                    error_message += "; workload rollback was incomplete"
                return
            with self._db() as connection:
                self._clusters.from_connection(connection).record_zoning_apply_in_connection(
                    connection,
                    cluster_id,
                    applied_mode=cluster["zoning"]["mode"],
                    applied_zones=cluster["zoning"]["zones"],
                    observed_zones=desired_zones,
                    status="disabled" if cluster["zoning"]["mode"] == "disabled" else "applied",
                    run_id=run_id,
                )
            succeeded = True
        except Exception as error:
            error_message = str(error)
            self._add_log(run_id, "Zoning runner error: " + error_message + "\n")
        finally:
            Path(inventory_path).unlink(missing_ok=True)
            with self._db() as connection:
                if not succeeded:
                    self._clusters.from_connection(connection).record_zoning_failure_in_connection(
                        connection, cluster_id, run_id, error_message
                    )
                self._finish(connection, run_id, "succeeded" if succeeded else "failed")

    async def _rollback_with(self, run_id, inventory_path, completed, previous_zones, reconcile) -> bool:
        succeeded = True
        for index, (assignment, payload) in enumerate(reversed(completed)):
            rollback_payload = json.loads(json.dumps(payload))
            rollback_payload["membership"]["zone_id"] = previous_zones.get(str(assignment["id"])) or ""
            if not await reconcile(run_id, inventory_path, rollback_payload, assignment["node_name"], f"rollback-{index}"):
                succeeded = False
        return succeeded

    def launch_apply(self, cluster_id: int) -> int:
        disabled_without_master = False
        with self._db() as connection:
            cluster, assignments = self.preflight(connection, cluster_id)
            if cluster["zoning"]["mode"] == "disabled" and not assignments:
                self._clusters.from_connection(connection).record_zoning_apply_in_connection(
                    connection,
                    cluster_id,
                    applied_mode="disabled",
                    applied_zones=[],
                    observed_zones={},
                    status="disabled",
                    run_id=None,
                )
                disabled_without_master = True
            else:
                run_id = self._start(
                    connection,
                    self._descriptor(
                        "zoning-apply",
                        cluster["name"] + ":zoning",
                        {"cluster_id": cluster_id, "mode": cluster["zoning"]["mode"], "zones": cluster["zoning"]["zones"]},
                    ),
                ).run_id
        if disabled_without_master:
            return self._completed_run(
                "zoning-apply",
                cluster["name"] + ":zoning",
                "Zoning is disabled and no Elasticsearch master is assigned.",
            )
        inventory_path = self._inventory(run_id)
        asyncio.create_task(self.run_apply(run_id, cluster_id, inventory_path))
        return run_id

    async def run_host_zone_change(self, run_id: int, node_id: int, previous_zone: str, zone_id: str, inventory_path: Path, *, reconcile=None) -> None:
        reconcile = reconcile or self._reconcile_runner or self.execute_reconcile
        succeeded = False
        completed: list[tuple[dict, dict]] = []
        error_message = "Host zone reconciliation failed"
        try:
            with self._db() as connection:
                assignments = []
                ids = self._workloads.from_connection(connection).active_elasticsearch_ids_for_node_in_connection(connection, node_id)
                for assignment_id in ids:
                    row = self._assignment_record(connection, assignment_id)
                    cluster = self._cluster_record(connection, row["cluster_id"])
                    if cluster["zoning"]["mode"] == "disabled":
                        continue
                    assignments.append(next(item for item in cluster["assignments"] if item["id"] == assignment_id))
            assignments.sort(key=lambda item: (item["cluster_id"], self._order.index(item["role"]), item["node_name"], item["id"]))
            for index, assignment in enumerate(assignments):
                with self._db() as connection:
                    payload = self._cluster_payload(connection, self._assignment_record(connection, assignment["id"]))
                if not await reconcile(run_id, inventory_path, payload, assignment["node_name"], f"host-{index}"):
                    error_message = f"Failed to apply zone {zone_id} to {assignment['role']} on {assignment['node_name']}"
                    previous = {str(item["id"]): previous_zone for item, _payload in completed}
                    if not await self._rollback_with(run_id, inventory_path, completed, previous, reconcile):
                        error_message += "; rollback was incomplete"
                    return
                completed.append((assignment, payload))
            with self._db() as connection:
                for cluster_id in {assignment["cluster_id"] for assignment in assignments}:
                    repository = self._clusters.from_connection(connection)
                    observation = repository.zoning_observation_record_in_connection(connection, cluster_id)
                    if not observation:
                        continue
                    observed = json.loads(observation["observed_zones_json"] or "{}")
                    for assignment in assignments:
                        if assignment["cluster_id"] == cluster_id:
                            observed[str(assignment["id"])] = zone_id
                    repository.update_observed_zones_in_connection(connection, cluster_id, observed, run_id)
            succeeded = True
        except Exception as error:
            error_message = str(error)
            self._add_log(run_id, "Host zone runner error: " + error_message + "\n")
        finally:
            Path(inventory_path).unlink(missing_ok=True)
            with self._db() as connection:
                if not succeeded:
                    self._hosts.from_connection(connection).restore_zone_in_connection(connection, node_id, previous_zone)
                self._workloads.from_connection(connection).clear_operation_for_node_in_connection(connection, node_id, run_id)
                self._finish(connection, run_id, "succeeded" if succeeded else "failed")
                if not succeeded:
                    self._append_log(connection, run_id, error_message.rstrip() + "\n")

# Compatibility alias used by the staged cluster service extraction.
ZoningService = ZoningWorker
