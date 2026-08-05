"""Workload topology and mutation HTTP routes with injected callbacks."""

import json
import sqlite3
from typing import Any, Callable, Mapping

from fastapi import APIRouter, Depends, HTTPException


def build_router(
    *,
    db_factory: Callable,
    user_dependency: Callable,
    cluster_record: Callable,
    render_topology: Callable,
    role_specs: Mapping[str, Any],
    elasticsearch_roles: Mapping[str, str],
    valid_ipv4: Callable,
) -> APIRouter:
    router = APIRouter()

    @router.get("/api/clusters/{cluster_id}/topology")
    async def cluster_topology(
        cluster_id: int,
        _: str = Depends(user_dependency),
    ):
        with db_factory() as connection:
            cluster = cluster_record(connection, cluster_id)
        topology, access_urls = render_topology(cluster, role_specs, elasticsearch_roles, valid_ipv4)
        return {"topology": topology, "access_urls": access_urls}

    return router


def build_mutation_router(
    *,
    db_factory: Callable,
    user_dependency: Callable,
    assignment_model: type,
    change_set_model: type,
    resource_model: type,
    membership_exists: Callable,
    node_enabled: Callable,
    validate_config: Callable,
    cluster_record: Callable,
    require_no_maintenance_conflict: Callable,
    require_cluster_capability: Callable,
    workload_mutation_capability: Any,
    conflict_message: Callable,
    seal_config: Callable,
    assignment_record: Callable,
    open_config: Callable,
    require_ready_membership: Callable,
    require_initial_master_batch: Callable,
    cluster_payload: Callable,
    launch_workload_change_batch: Callable,
    launch: Callable,
    reconcile_command: Callable,
    repository_factory: Callable,
) -> APIRouter:
    """Build cluster-qualified workload mutation routes.

    The route layer owns HTTP validation and response shape only.  Database,
    orchestration, and run behavior stay behind callbacks so the compatibility
    implementation can be retired without changing the public API.
    """

    router = APIRouter()

    @router.post("/api/clusters/{cluster_id}/assignments", status_code=201)
    async def add_assignment(
        cluster_id: int,
        input: assignment_model,
        _: str = Depends(user_dependency),
    ):
        validate_config(input.role, input.config)
        with db_factory() as connection:
            repository = repository_factory(connection)
            cluster_record(connection, cluster_id)
            require_no_maintenance_conflict(connection, cluster_id=cluster_id)
            require_cluster_capability(connection, cluster_id, workload_mutation_capability)
            if not membership_exists(connection, cluster_id, input.node_id):
                raise HTTPException(422, "Add the host to this cluster first")
            conflict = conflict_message(connection, cluster_id, input.node_id, input.role)
            if conflict:
                raise HTTPException(409, conflict)
            try:
                assignment_id = repository.upsert_assignment_in_connection(
                    connection,
                    cluster_id=cluster_id,
                    node_id=input.node_id,
                    role=input.role,
                    config_json=seal_config(json.dumps(input.config)),
                )
            except sqlite3.IntegrityError as error:
                raise HTTPException(409, "Assignment could not be created") from error
        return {"id": assignment_id}

    @router.post("/api/clusters/{cluster_id}/workload-changes/apply")
    async def apply_workload_changes(
        cluster_id: int,
        input: change_set_model,
        _: str = Depends(user_dependency),
    ):
        with db_factory() as connection:
            require_no_maintenance_conflict(connection, cluster_id=cluster_id)
            require_cluster_capability(connection, cluster_id, workload_mutation_capability)
        return {"run_id": launch_workload_change_batch(cluster_id, input)}

    @router.put("/api/assignments/{assignment_id}/resources")
    async def update_resources(
        assignment_id: int,
        input: resource_model,
        _: str = Depends(user_dependency),
    ):
        update = input.model_dump(exclude_none=True)
        with db_factory() as connection:
            repository = repository_factory(connection)
            row = assignment_record(connection, assignment_id)
            require_no_maintenance_conflict(connection, assignment_id=assignment_id)
            require_cluster_capability(connection, row["cluster_id"], workload_mutation_capability)
            if row["operation_run_id"]:
                raise HTTPException(409, "This workload is already part of an active change set")
            require_ready_membership(row)
            previous = open_config(row["config_json"])
            config = {**previous, **update}
            validate_config(row["role"], config)
            repository.update_assignment_config_in_connection(connection, assignment_id, seal_config(json.dumps(config)))
            row = assignment_record(connection, assignment_id)
            payload = cluster_payload(connection, row)
            target = f"{row['cluster_name']}:{row['node_name']}:{row['role']}:resources"
            name = row["node_name"]
            cluster_id = row["cluster_id"]
        return {
            "run_id": launch(
                "resource-update",
                target,
                lambda inv, variables_path: reconcile_command(inv, variables_path, name),
                variables=payload,
                context={
                    "rollback_assignment_id": assignment_id,
                    "previous_config": previous,
                    "filebeat_reconcile_cluster_id": cluster_id,
                },
            )
        }

    @router.post("/api/assignments/{assignment_id}/apply")
    async def apply_assignment(
        assignment_id: int,
        _: str = Depends(user_dependency),
    ):
        with db_factory() as connection:
            row = assignment_record(connection, assignment_id)
            require_no_maintenance_conflict(connection, assignment_id=assignment_id)
            require_cluster_capability(connection, row["cluster_id"], workload_mutation_capability)
            if row["operation_run_id"]:
                raise HTTPException(409, "This workload is already part of an active change set")
            if not node_enabled(connection, row["node_id"]):
                raise HTTPException(422, "Enable the host before applying a role")
            payload = cluster_payload(connection, row)
            require_initial_master_batch(connection, row)
            target = f"{row['cluster_name']}:{row['node_name']}:{row['role']}"
            name = row["node_name"]
            cluster_id = row["cluster_id"]
        return {
            "run_id": launch(
                "reconcile",
                target,
                lambda inv, variables_path: reconcile_command(inv, variables_path, name),
                variables=payload,
                context={"filebeat_reconcile_cluster_id": cluster_id},
            )
        }

    @router.delete("/api/assignments/{assignment_id}")
    async def remove_assignment(
        assignment_id: int,
        _: str = Depends(user_dependency),
        mode: str = "detach",
    ):
        if mode not in {"detach", "purge"}:
            raise HTTPException(422, "Removal mode must be detach or purge")
        with db_factory() as connection:
            repository = repository_factory(connection)
            row = assignment_record(connection, assignment_id)
            require_no_maintenance_conflict(connection, assignment_id=assignment_id)
            require_cluster_capability(connection, row["cluster_id"], workload_mutation_capability)
            if row["operation_run_id"]:
                raise HTTPException(409, "This workload is already part of an active change set")
            if row["role"] == "master" and repository.initial_master_has_dependents_in_connection(
                connection,
                cluster_id=row["cluster_id"],
                assignment_id=assignment_id,
            ):
                raise HTTPException(409, "Detach or purge dependent cluster roles before removing the initial master")
            if mode == "detach":
                repository.delete_assignment_in_connection(connection, assignment_id)
                return {"detached": True}
            payload = cluster_payload(connection, row, "purge")
            target = f"{row['cluster_name']}:{row['node_name']}:{row['role']}:purge"
            name = row["node_name"]
            cluster_id = row["cluster_id"]
        return {
            "run_id": launch(
                "purge",
                target,
                lambda inv, variables_path: reconcile_command(inv, variables_path, name),
                variables=payload,
                context={"purge_assignment_id": assignment_id, "filebeat_reconcile_cluster_id": cluster_id},
            )
        }

    return router


def build_legacy_compatibility_router(*, user_dependency: Callable) -> APIRouter:
    """Keep retired node-role endpoints stable outside the application hub."""

    router = APIRouter()

    @router.post("/api/nodes/{node_id}/roles")
    async def legacy_add_role(node_id: int, _: str = Depends(user_dependency)):
        raise HTTPException(410, "Use cluster-qualified assignments")

    @router.delete("/api/nodes/{node_id}/roles/{role}")
    async def legacy_remove_role(node_id: int, role: str, _: str = Depends(user_dependency)):
        raise HTTPException(410, "Use cluster-qualified assignments")

    return router
