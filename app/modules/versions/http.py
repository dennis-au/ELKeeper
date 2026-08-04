"""Version-management HTTP routes with injected application callbacks."""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Mapping

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field


class VersionTargetInput(BaseModel):
    target_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")


def build_router(
    *,
    db_factory: Callable,
    user_dependency: Callable,
    role_specs: Mapping[str, Any],
    cluster_record: Callable,
    version_details: Callable,
    available_role_versions: Callable,
    available_versions: Callable,
    recommended_version: Callable,
    validate_version_target: Callable,
    image_for_role: Callable,
    metricbeat_roles: set[str] | frozenset[str],
    metricbeat_image: Callable,
    filebeat_enabled_image: Callable,
    launch_commands: Callable,
    probe_command: Callable,
    record_observation: Callable,
    download_command: Callable,
    launch_upgrade: Callable,
    active_operation_checker: Callable[[str], bool],
    require_no_maintenance_conflict: Callable,
    require_cluster_capability: Callable,
    workload_mutation_capability: Any,
    lifecycle_capability: Any,
    default_stack_version: str,
) -> APIRouter:
    router = APIRouter()

    @router.get("/api/clusters/{cluster_id}/versions")
    async def get_cluster_versions(
        cluster_id: int,
        _: str = Depends(user_dependency),
        role: str | None = None,
    ):
        if role is not None and role not in role_specs:
            raise HTTPException(422, "Unknown workload role")
        with db_factory() as connection:
            details = version_details(connection, cluster_id, include_candidates=False)
            cluster = cluster_record(connection, cluster_id)
        try:
            if role:
                details["available_versions"] = await asyncio.to_thread(
                    available_role_versions, role, details["assignments"]
                )
            else:
                details["available_versions"] = await asyncio.to_thread(
                    available_versions,
                    details["assignments"],
                    cluster["log_monitoring"]["filebeat_enabled"],
                )
        except HTTPException as error:
            details["registry_error"] = error.detail
        details["recommended_version"] = (
            recommended_version(details["assignments"], details["available_versions"])
            or cluster["desired_version"]
            or default_stack_version
        )
        return details

    @router.post("/api/clusters/{cluster_id}/versions/refresh")
    async def refresh_cluster_versions(
        cluster_id: int,
        _: str = Depends(user_dependency),
    ):
        with db_factory() as connection:
            cluster = cluster_record(connection, cluster_id)
            if not cluster["assignments"]:
                raise HTTPException(422, "Assign workloads before refreshing component versions")
            if active_operation_checker(cluster["name"]):
                raise HTTPException(409, "Wait for the active cluster operation to finish")
        return {
            "run_id": launch_commands(
                "version-probe",
                cluster["name"] + ":versions",
                lambda inventory: [
                    (probe_command(inventory, cluster, assignment), {"assignment_id": assignment["id"]})
                    for assignment in cluster["assignments"]
                ],
                record_observation,
            )
        }

    @router.post("/api/clusters/{cluster_id}/versions/download")
    async def download_cluster_versions(
        cluster_id: int,
        input: VersionTargetInput,
        _: str = Depends(user_dependency),
    ):
        with db_factory() as connection:
            cluster = cluster_record(connection, cluster_id)
            require_no_maintenance_conflict(connection, cluster_id=cluster_id)
            require_cluster_capability(connection, cluster_id, workload_mutation_capability)
            if active_operation_checker(cluster["name"]):
                raise HTTPException(409, "Wait for the active cluster operation to finish")
        candidates = await asyncio.to_thread(
            available_versions,
            cluster["assignments"],
            cluster["log_monitoring"]["filebeat_enabled"],
        )
        validate_version_target(cluster, input.target_version, candidates)
        images: dict[tuple[str, str], bool] = {}
        for assignment in cluster["assignments"]:
            images[(assignment["node_name"], image_for_role(assignment["role"], input.target_version))] = True
            if assignment["role"] in metricbeat_roles:
                images[(assignment["node_name"], metricbeat_image(input.target_version))] = True
            if cluster["log_monitoring"]["filebeat_enabled"]:
                images[(assignment["node_name"], filebeat_enabled_image(input.target_version))] = True
        return {
            "run_id": launch_commands(
                "version-download",
                cluster["name"] + ":download:" + input.target_version,
                lambda inventory: [
                    (download_command(inventory, node_name, image), {"node_name": node_name, "image": image})
                    for node_name, image in sorted(images)
                ],
            )
        }

    @router.post("/api/clusters/{cluster_id}/upgrades")
    async def upgrade_cluster(
        cluster_id: int,
        input: VersionTargetInput,
        _: str = Depends(user_dependency),
    ):
        with db_factory() as connection:
            cluster = cluster_record(connection, cluster_id)
            require_cluster_capability(connection, cluster_id, workload_mutation_capability)
            require_cluster_capability(connection, cluster_id, lifecycle_capability)
        candidates = await asyncio.to_thread(
            available_versions,
            cluster["assignments"],
            cluster["log_monitoring"]["filebeat_enabled"],
        )
        return {"run_id": launch_upgrade(cluster_id, input.target_version, candidates)}

    return router
