"""Public cluster-zoning operation facade.

This keeps the ZoningWorker implementation and its sequencing contract inside
the clusters module while allowing the legacy application assembly callbacks
to remain injectable during migration.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .zoning import ZoningWorker


class ZoningOperations:
    """Own construction and lifecycle calls for cluster zoning operations."""

    def __init__(self, **dependencies: Any) -> None:
        self._dependencies = dict(dependencies)

    def _worker(self, *, reconcile_runner: Callable | None = None, settings_runner: Callable | None = None) -> ZoningWorker:
        dependencies = dict(self._dependencies)
        if reconcile_runner is not None:
            dependencies["reconcile_runner"] = reconcile_runner
        if settings_runner is not None:
            dependencies["settings_runner"] = settings_runner
        return ZoningWorker(**dependencies)

    def assignments(self, cluster: dict) -> list[dict]:
        return self._worker().assignments(cluster)

    def preflight(self, connection: Any, cluster_id: int):
        return self._worker().preflight(connection, cluster_id)

    async def execute_reconcile(self, run_id: int, inventory: Path, payload: dict, name: str, suffix: str) -> bool:
        return await self._worker().execute_reconcile(run_id, inventory, payload, name, suffix)

    async def execute_settings(self, run_id: int, inventory: Path, payload: dict, name: str) -> bool:
        return await self._worker().execute_settings(run_id, inventory, payload, name)

    def settings_payload(self, connection: Any, cluster: dict) -> tuple[dict, str]:
        return self._worker().settings_payload(connection, cluster)

    async def rollback(self, run_id: int, inventory: Path, completed: list, previous_zones: dict, *, reconcile: Callable) -> bool:
        return await self._worker().rollback(run_id, inventory, completed, previous_zones, reconcile=reconcile)

    async def run_apply(self, run_id: int, cluster_id: int, inventory: Path, *, reconcile: Callable, settings: Callable) -> None:
        await self._worker().run_apply(run_id, cluster_id, inventory, reconcile=reconcile, settings=settings)

    def launch_apply(self, cluster_id: int) -> int:
        return self._worker().launch_apply(cluster_id)

    async def run_host_zone_change(
        self,
        run_id: int,
        node_id: int,
        previous_zone: str,
        zone_id: str,
        inventory: Path,
        *,
        reconcile: Callable,
    ) -> None:
        await self._worker().run_host_zone_change(
            run_id,
            node_id,
            previous_zone,
            zone_id,
            inventory,
            reconcile=reconcile,
        )


__all__ = ["ZoningOperations"]
