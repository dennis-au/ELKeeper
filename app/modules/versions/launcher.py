"""Version-operation launch boundary."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from typing import Any

from fastapi import HTTPException


class VersionUpgradeLauncher:
    """Create and schedule one guarded cluster upgrade run.

    All application-specific orchestration is injected. This keeps version
    policy and run creation behind a public contract without importing the
    FastAPI assembly module.
    """

    def __init__(
        self,
        *,
        db_factory: Callable,
        cluster_record: Callable,
        preflight: Callable,
        active_operation: Callable[[Any, str], bool],
        upgrade_order: Sequence[str],
        start_run: Callable,
        run_descriptor: Callable,
        inventory: Callable[[int], Any],
        run_upgrade: Callable[..., Any],
        schedule: Callable[[Any], Any] = asyncio.create_task,
    ):
        self._db = db_factory
        self._cluster_record = cluster_record
        self._preflight = preflight
        self._active_operation = active_operation
        self._upgrade_order = tuple(upgrade_order)
        self._start_run = start_run
        self._run_descriptor = run_descriptor
        self._inventory = inventory
        self._run_upgrade = run_upgrade
        self._schedule = schedule

    def launch(self, cluster_id: int, target_version: str, candidates: list[str] | None = None) -> int:
        with self._db() as connection:
            cluster = self._cluster_record(connection, cluster_id)
            if not cluster:
                raise HTTPException(404, "Cluster not found")
            if self._active_operation(connection, cluster["name"]):
                raise HTTPException(409, "Wait for the active cluster operation to finish")
            major_upgrade = self._preflight(cluster, target_version, candidates)
            order = {role: index for index, role in enumerate(self._upgrade_order)}
            ordered = sorted(
                cluster["assignments"],
                key=lambda item: (order.get(item["role"], len(order)), item["node_name"], item["id"]),
            )
            run_id = self._start_run(
                connection,
                self._run_descriptor(
                    "upgrade",
                    cluster["name"] + ":upgrade:" + target_version,
                    {"target_version": target_version, "major_upgrade": major_upgrade},
                ),
            ).run_id
        inventory_path = self._inventory(run_id)
        self._schedule(
            self._run_upgrade(
                run_id,
                cluster_id,
                target_version,
                inventory_path,
                [assignment["id"] for assignment in ordered],
            )
        )
        return run_id
