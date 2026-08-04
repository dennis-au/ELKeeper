"""Cluster inventory, settings, and membership HTTP contracts."""

import inspect
import json
import sqlite3
from typing import Any, Callable

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import ValidationError

from app.modules.platform import require_user


def build_settings_router(
    *,
    settings_model: type,
    get_settings: Callable[[int], dict],
    update_settings: Callable[[int, Any, str], Any],
    user_dependency: Callable = require_user,
) -> APIRouter:
    router = APIRouter()

    @router.get("/api/clusters/{cluster_id}/settings")
    async def cluster_settings(cluster_id: int, _: str = Depends(user_dependency)):
        result = get_settings(cluster_id)
        return await result if inspect.isawaitable(result) else result

    @router.put("/api/clusters/{cluster_id}/settings")
    async def update_cluster_settings(
        cluster_id: int,
        settings: dict = Body(...),
        username: str = Depends(user_dependency),
    ):
        try:
            validated = settings_model.model_validate(settings)
        except ValidationError as error:
            raise HTTPException(422, error.errors()) from error
        result = update_settings(cluster_id, validated, username)
        return await result if inspect.isawaitable(result) else result

    return router


__all__ = ["build_settings_router"]


def build_inventory_router(
    *,
    list_clusters: Callable[[], list[dict]],
    get_cluster: Callable[[int], dict],
    user_dependency: Callable = require_user,
) -> APIRouter:
    """Build read-only cluster inventory routes.

    Reads stay behind callbacks so the repository/DTO assembly remains owned by
    the clusters module while legacy callers can still provide their existing
    projection implementation.
    """

    router = APIRouter()

    @router.get("/api/clusters")
    async def clusters(_: str = Depends(user_dependency)):
        return list_clusters()

    @router.get("/api/clusters/{cluster_id}")
    async def cluster(cluster_id: int, _: str = Depends(user_dependency)):
        return get_cluster(cluster_id)

    return router


__all__.append("build_inventory_router")


def build_membership_router(
    *,
    db_factory: Callable,
    user_dependency: Callable = require_user,
    membership_model: type,
    validate_membership_network: Callable,
    cluster_record: Callable,
    require_no_maintenance_conflict: Callable,
    require_cluster_capability: Callable,
    workload_mutation_capability: Any,
    require_cluster_host_zone: Callable,
    node_record: Callable,
    insert_membership: Callable,
    update_membership: Callable,
    has_assignments: Callable,
    delete_membership: Callable,
) -> APIRouter:
    """Build cluster membership lifecycle routes behind public callbacks."""

    router = APIRouter()

    @router.post("/api/clusters/{cluster_id}/members", status_code=201)
    async def add_member(
        cluster_id: int,
        input: membership_model,
        _: str = Depends(user_dependency),
    ):
        validate_membership_network(input)
        with db_factory() as connection:
            cluster = cluster_record(connection, cluster_id)
            require_no_maintenance_conflict(connection, cluster_id=cluster_id)
            require_cluster_capability(connection, cluster_id, workload_mutation_capability)
            node = node_record(connection, input.node_id)
            if not node:
                raise HTTPException(404, "Node not found")
            if not node["enabled"]:
                raise HTTPException(422, "Enable the node before adding it to a cluster")
            require_cluster_host_zone(cluster, node)
            try:
                insert_membership(connection, cluster_id, input)
            except sqlite3.IntegrityError as error:
                raise HTTPException(409, "Host is already in this cluster") from error
        return {"added": True}

    @router.put("/api/clusters/{cluster_id}/members/{node_id}")
    async def update_member(
        cluster_id: int,
        node_id: int,
        input: membership_model,
        _: str = Depends(user_dependency),
    ):
        if input.node_id != node_id:
            raise HTTPException(422, "Membership node does not match the request path")
        validate_membership_network(input)
        with db_factory() as connection:
            cluster = cluster_record(connection, cluster_id)
            require_no_maintenance_conflict(connection, cluster_id=cluster_id)
            require_cluster_capability(connection, cluster_id, workload_mutation_capability)
            node = node_record(connection, node_id)
            if not node:
                raise HTTPException(404, "Node not found")
            require_cluster_host_zone(cluster, node)
            if not update_membership(connection, cluster_id, node_id, input):
                raise HTTPException(404, "Cluster membership not found")
        return {"updated": True}

    @router.delete("/api/clusters/{cluster_id}/members/{node_id}", status_code=204)
    async def remove_member(
        cluster_id: int,
        node_id: int,
        _: str = Depends(user_dependency),
    ):
        with db_factory() as connection:
            require_no_maintenance_conflict(connection, cluster_id=cluster_id)
            require_cluster_capability(connection, cluster_id, workload_mutation_capability)
            if has_assignments(connection, cluster_id, node_id):
                raise HTTPException(409, "Detach or purge the host roles first")
            delete_membership(connection, cluster_id, node_id)

    return router


__all__.append("build_membership_router")


def build_log_monitoring_router(
    *,
    db_factory: Callable,
    user_dependency: Callable,
    input_model: type,
    cluster_record: Callable,
    require_no_maintenance_conflict: Callable,
    require_cluster_capability: Callable,
    workload_mutation_capability: Any,
    active_cluster_operation: Callable,
    retention_days: int,
    audit_event: Callable,
    launch_reconcile: Callable,
    update_observability: Callable,
) -> APIRouter:
    """Build cluster log-monitoring routes around the cluster contract."""

    router = APIRouter()

    @router.get("/api/clusters/{cluster_id}/log-monitoring")
    async def get_cluster_log_monitoring(
        cluster_id: int,
        _: str = Depends(user_dependency),
    ):
        with db_factory() as connection:
            return cluster_record(connection, cluster_id)["log_monitoring"]

    @router.put("/api/clusters/{cluster_id}/log-monitoring")
    async def update_cluster_log_monitoring(
        cluster_id: int,
        input: input_model,
        username: str = Depends(user_dependency),
    ):
        with db_factory() as connection:
            cluster = cluster_record(connection, cluster_id)
            require_no_maintenance_conflict(connection, cluster_id=cluster_id)
            require_cluster_capability(connection, cluster_id, workload_mutation_capability)
            if active_cluster_operation(connection, cluster["name"]):
                raise HTTPException(409, "Wait for the active cluster operation to finish")
            settings = {
                "filebeat_enabled": input.filebeat_enabled,
                "retention_days": retention_days,
            }
            update_observability(connection, cluster_id, json.dumps(settings, sort_keys=True))
        audit_event(
            username,
            "cluster_log_monitoring_updated",
            str(cluster_id),
            "enabled" if input.filebeat_enabled else "disabled",
        )
        return {"run_id": launch_reconcile(cluster_id, username)}

    return router


__all__.append("build_log_monitoring_router")


def build_zoning_router(
    *,
    db_factory: Callable,
    user_dependency: Callable,
    zoning_model: type,
    cluster_record: Callable,
    require_no_maintenance_conflict: Callable,
    require_cluster_capability: Callable,
    cluster_settings_capability: Any,
    validate_catalog_update: Callable,
    update_zoning: Callable,
    audit_event: Callable,
    completed_run: Callable,
    launch_apply: Callable,
) -> APIRouter:
    """Build desired zoning and reconciliation routes for a cluster."""

    router = APIRouter()

    @router.get("/api/clusters/{cluster_id}/zoning")
    async def get_cluster_zoning(cluster_id: int, _: str = Depends(user_dependency)):
        with db_factory() as connection:
            cluster = cluster_record(connection, cluster_id)
        return {
            "cluster_id": cluster_id,
            "zoning": cluster["zoning"],
            "status": cluster["zoning_status"],
        }

    @router.put("/api/clusters/{cluster_id}/zoning")
    async def update_cluster_zoning(
        cluster_id: int,
        zoning: zoning_model,
        username: str = Depends(user_dependency),
    ):
        with db_factory() as connection:
            cluster = cluster_record(connection, cluster_id)
            require_no_maintenance_conflict(connection, cluster_id=cluster_id)
            require_cluster_capability(connection, cluster_id, cluster_settings_capability)
            validate_catalog_update(connection, cluster_id, zoning)
            update_zoning(connection, cluster_id, zoning.model_dump_json())
        audit_event(
            username,
            "cluster_zoning_updated",
            cluster_id,
            str(cluster_id),
            zoning.mode + ":" + ",".join(zoning.zones),
        )
        run_id = completed_run(
            "zoning-config",
            cluster["name"] + ":zoning",
            "Stored desired cluster zoning configuration.",
            {"cluster_id": cluster_id, "mode": zoning.mode, "zones": zoning.zones},
        )
        return {
            "updated": True,
            "run_id": run_id,
            "apply_required": zoning.model_dump() != cluster["zoning"],
        }

    @router.post("/api/clusters/{cluster_id}/zoning/apply")
    async def apply_cluster_zoning(cluster_id: int, _: str = Depends(user_dependency)):
        with db_factory() as connection:
            require_no_maintenance_conflict(connection, cluster_id=cluster_id)
            require_cluster_capability(connection, cluster_id, cluster_settings_capability)
        return {"run_id": launch_apply(cluster_id)}

    return router


__all__.append("build_zoning_router")


def build_lifecycle_router(
    *,
    cluster_input_model: type,
    provider_update_model: type,
    create_cluster: Callable[[Any], dict],
    update_cluster: Callable[[int, Any], dict],
    delete_cluster: Callable[[int], None],
    get_provider: Callable[[int], dict],
    update_provider: Callable[[int, Any, str], dict],
    user_dependency: Callable = require_user,
) -> APIRouter:
    """Expose cluster lifecycle routes through the clusters boundary.

    The application assembly supplies the compatibility implementation while
    persistence and policy logic continue to move into ``ClusterService``.
    Keeping the handlers here fixes route ownership without changing request
    DTOs, response shapes, or existing test patch points.
    """

    router = APIRouter()

    @router.post("/api/clusters", status_code=201)
    async def create(input: cluster_input_model, _: str = Depends(user_dependency)):
        result = create_cluster(input)
        return await result if inspect.isawaitable(result) else result

    @router.get("/api/clusters/{cluster_id}/provider")
    async def provider(cluster_id: int, _: str = Depends(user_dependency)):
        result = get_provider(cluster_id)
        return await result if inspect.isawaitable(result) else result

    @router.put("/api/clusters/{cluster_id}/provider")
    async def update_provider_route(
        cluster_id: int,
        input: provider_update_model,
        username: str = Depends(user_dependency),
    ):
        result = update_provider(cluster_id, input, username)
        return await result if inspect.isawaitable(result) else result

    @router.put("/api/clusters/{cluster_id}")
    async def update(cluster_id: int, input: cluster_input_model, _: str = Depends(user_dependency)):
        result = update_cluster(cluster_id, input)
        return await result if inspect.isawaitable(result) else result

    @router.delete("/api/clusters/{cluster_id}", status_code=204)
    async def remove(cluster_id: int, _: str = Depends(user_dependency)):
        result = delete_cluster(cluster_id)
        if inspect.isawaitable(result):
            await result

    return router


__all__.append("build_lifecycle_router")
