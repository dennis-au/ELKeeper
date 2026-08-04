"""Application assembly for the public version-operation contract.

The versions module owns the runtime probe, policy, upgrade worker, and launch
ordering.  The controller only supplies persistence and orchestration seams;
it does not need to construct those private implementation objects itself.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable, Sequence

from .launcher import VersionUpgradeLauncher
from .runtime import VersionRuntimeService
from .upgrade import VersionUpgradeService
from .worker import VersionUpgradeWorker


class VersionOperations:
    """Public application-facing facade for version lifecycle operations."""

    def __init__(
        self,
        *,
        ansible: Callable,
        workload_name: Callable,
        image_for_role: Callable,
        image_version: Callable,
        default_stack_version: str,
        repository_factory: Callable,
        cluster_record: Callable,
        available_versions: Callable,
        version_key: Callable,
        membership_ready: Callable,
        observation_is_fresh: Callable,
        topology_elasticsearch_roles: set[str],
        db_factory: Callable,
        variables_dir: Path,
        assignment_record: Callable,
        cluster_payload: Callable,
        reconcile_command: Callable,
        upgrade_preflight_command: Callable,
        execute_logged_command: Callable,
        add_log: Callable,
        platform_finish_run: Callable,
        workload_repository: Callable,
        launch_filebeat_reconcile: Callable,
        active_operation: Callable,
        upgrade_order: Sequence[str],
        start_run: Callable,
        run_descriptor: Callable,
        inventory: Callable,
        schedule: Callable = asyncio.create_task,
    ) -> None:
        self._runtime = VersionRuntimeService(
            ansible=ansible,
            workload_name=workload_name,
            image_for_role=image_for_role,
            image_version=image_version,
            default_stack_version=default_stack_version,
            repository_factory=repository_factory,
        )
        self._policy = VersionUpgradeService(
            cluster_record=cluster_record,
            available_versions=available_versions,
            default_stack_version=default_stack_version,
            version_key=version_key,
            membership_ready=membership_ready,
            observation_is_fresh=observation_is_fresh,
            topology_elasticsearch_roles=topology_elasticsearch_roles,
        )
        self._worker = VersionUpgradeWorker(
            db_factory=db_factory,
            variables_dir=variables_dir,
            assignment_record=assignment_record,
            cluster_record=cluster_record,
            cluster_payload=cluster_payload,
            reconcile_command=reconcile_command,
            upgrade_preflight_command=upgrade_preflight_command,
            execute_logged_command=execute_logged_command,
            add_log=add_log,
            platform_finish_run=platform_finish_run,
            workload_repository=workload_repository,
            version_key=version_key,
            launch_filebeat_reconcile=launch_filebeat_reconcile,
        )
        self._launcher = VersionUpgradeLauncher(
            db_factory=db_factory,
            cluster_record=cluster_record,
            preflight=self.preflight,
            active_operation=active_operation,
            upgrade_order=upgrade_order,
            start_run=start_run,
            run_descriptor=run_descriptor,
            inventory=inventory,
            run_upgrade=self.run_upgrade,
            schedule=schedule,
        )

    def probe_command(self, inventory: Any, cluster: dict, assignment: dict) -> list[str]:
        return self._runtime.probe_command(inventory, cluster, assignment)

    def record_observation(self, metadata: dict, output: str, succeeded: bool) -> None:
        self._runtime.record_observation(metadata, output, succeeded)

    def download_command(self, inventory: Any, node_name: str, image: str) -> list[str]:
        return self._runtime.download_command(inventory, node_name, image)

    def details(self, connection: Any, cluster_id: int, *, include_candidates: bool = True) -> dict:
        return self._policy.details(connection, cluster_id, include_candidates=include_candidates)

    def validate_target(self, cluster: dict, target_version: str, candidates: list[str] | None = None) -> bool:
        return self._policy.validate_target(cluster, target_version, candidates)

    def preflight(self, cluster: dict, target_version: str, candidates: list[str] | None = None) -> bool:
        return self._policy.preflight(cluster, target_version, candidates)

    async def run_upgrade(
        self,
        run_id: int,
        cluster_id: int,
        target_version: str,
        inventory_path: Path,
        assignment_ids: list[int],
    ) -> None:
        await self._worker.run(run_id, cluster_id, target_version, inventory_path, assignment_ids)

    def launch_upgrade(self, cluster_id: int, target_version: str, candidates: list[str] | None = None) -> int:
        return self._launcher.launch(cluster_id, target_version, candidates)
