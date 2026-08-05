"""Host inventory, lifecycle, enrollment, and storage routes."""

import asyncio
import json
import secrets
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import ValidationError

from app.modules.platform import RunDescriptor, require_user, start_run_in_connection, write_event_in_connection
from app.modules.workloads import WorkloadRepository

from .repository import HostRepository


def build_router(
    *,
    host_provider: Callable[[int], dict | None],
    remote_command: Callable[..., object],
    storage_renderer: Callable[[dict], list[dict]],
    user_dependency: Callable = require_user,
) -> APIRouter:
    router = APIRouter()

    @router.get("/api/nodes/{node_id}/storage")
    async def node_storage(node_id: int, _: str = Depends(user_dependency)):
        node = host_provider(node_id)
        if not node or not node.get("enabled"):
            raise HTTPException(404, "Enabled host not found")
        try:
            output = await remote_command(
                node,
                "findmnt",
                "--json",
                "--bytes",
                "--real",
                "--output",
                "TARGET,SOURCE,FSTYPE,OPTIONS,SIZE,AVAIL",
                timeout=12,
            )
            payload = json.loads(output)
            if not isinstance(payload, dict):
                raise ValueError("Host storage inventory has an invalid format")
        except (json.JSONDecodeError, RuntimeError, UnicodeDecodeError, ValueError) as error:
            raise HTTPException(503, f"Could not inventory host storage: {str(error)[:160]}") from error
        from datetime import datetime, timezone

        observed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        return {"node_id": node_id, "observed_at": observed_at, "mounts": storage_renderer(payload)}

    return router


def build_lifecycle_router(
    *,
    enabled_host_provider: Callable[[int], dict],
    require_no_conflict: Callable[[int], None],
    has_assignments: Callable[[int], bool],
    launch_action: Callable[[dict, str], int],
    user_dependency: Callable = require_user,
) -> APIRouter:
    """Build host lifecycle routes around the host orchestration boundary."""

    router = APIRouter()

    async def run_host_action(node_id: int, action: str):
        node = enabled_host_provider(node_id)
        require_no_conflict(node_id)
        if action == "deinitialize" and has_assignments(node_id):
            raise HTTPException(409, "Detach or purge all managed workloads before de-initializing this host")
        return {"run_id": launch_action(node, action)}

    @router.post("/api/nodes/{node_id}/initialize")
    async def initialize_node(node_id: int, _: str = Depends(user_dependency)):
        return await run_host_action(node_id, "initialize")

    @router.post("/api/nodes/{node_id}/reboot")
    async def reboot_node(node_id: int, _: str = Depends(user_dependency)):
        return await run_host_action(node_id, "reboot")

    @router.post("/api/nodes/{node_id}/deinitialize")
    async def deinitialize_node(node_id: int, _: str = Depends(user_dependency)):
        return await run_host_action(node_id, "deinitialize")

    return router


def build_batch_router(
    *,
    batch_model: type,
    db_factory: Callable,
    require_no_conflict: Callable[[int], None],
    enabled_host_names: Callable[[object, list[int]], list[str]],
    launch_action: Callable[[str], int],
    user_dependency: Callable = require_user,
) -> APIRouter:
    """Build the multi-host initialization route.

    Host selection and run creation remain behind host/orchestration contracts;
    this module only owns request validation and the stable response shape.
    """

    router = APIRouter()

    @router.post("/api/hosts/initialize")
    async def initialize_hosts(input: batch_model, _: str = Depends(user_dependency)):
        with db_factory() as connection:
            for node_id in input.node_ids:
                require_no_conflict(node_id)
            names = enabled_host_names(connection, input.node_ids)
        return {"run_ids": [launch_action(name) for name in names]}

    return router


def build_inventory_router(
    *,
    node_model: type,
    list_nodes: Callable[[], list[dict]],
    create_node: Callable[[dict], int],
    user_dependency: Callable = require_user,
) -> APIRouter:
    """Build host inventory routes around the host repository contract."""

    router = APIRouter()

    @router.get("/api/nodes")
    async def nodes(_: str = Depends(user_dependency)):
        return list_nodes()

    @router.post("/api/nodes", status_code=201)
    async def create(input: dict, _: str = Depends(user_dependency)):
        try:
            validated = node_model.model_validate(input)
        except ValidationError as error:
            detail = [
                {"loc": item.get("loc", ()), "msg": item.get("msg", "Invalid value"), "type": item.get("type", "value_error")}
                for item in error.errors()
            ]
            raise HTTPException(422, detail) from error
        try:
            return {"id": create_node(validated.model_dump())}
        except Exception as error:
            # Repository implementations expose sqlite integrity errors without
            # coupling this HTTP module to a concrete database driver.
            if error.__class__.__name__ == "IntegrityError":
                raise HTTPException(409, "Node name already exists") from error
            raise

    return router


def build_management_router(
    *,
    password_test_model: type,
    enrollment_model: type,
    key_install_model: type,
    node_update_model: type,
    zone_model: type,
    db_factory: Callable,
    normalize_host_key: Callable[[str | None], str],
    password_test: Callable[[dict, str], tuple[bool, str]],
    enrollment_key: Callable[[], dict | None],
    launch_password_enrollment: Callable[[dict, str, bool, str, bool], int],
    launch_key_enrollment_probe: Callable[[dict, str, bool], int],
    require_no_conflict: Callable[[object, int], None],
    validate_zone_change: Callable[[object, int, int, str], None],
    fingerprint: Callable[[str], str],
    controller_key_rows: Callable[[], tuple[dict | None, dict | None]],
    launch_key_revocation: Callable[[dict, dict, int], int],
    launch_probe: Callable[[str], int],
    completed_run: Callable[[str, str, str, dict | None], int],
    inventory_for_run: Callable[[int], object],
    run_zone_change: Callable[[int, int, str | None, str, object], object],
    audit_fn: Callable[[str, str, str, str], None],
    has_membership: Callable[[object, int], bool],
    user_dependency: Callable = require_user,
) -> APIRouter:
    """Build the mutating host inventory and enrollment compatibility routes.

    The surrounding application supplies orchestration and cross-domain
    contracts.  This module owns host request handling and ``nodes`` writes
    without importing private cluster, workload, or controller-key details.
    """

    router = APIRouter()

    @router.get("/api/hosts/ssh-host-keys")
    async def host_key_records(_: str = Depends(user_dependency)):
        with db_factory() as connection:
            records = HostRepository.from_connection(connection).host_key_records_in_connection(connection)
        return {
            "items": [
                {
                    "node_id": record["id"],
                    "name": record["name"],
                    "address": record["address"],
                    "ssh_port": record["ssh_port"],
                    "fingerprint": fingerprint(record["ssh_host_key"]),
                }
                for record in records
            ]
        }

    @router.post("/api/nodes/test-password")
    async def test_node_password(input: password_test_model, username: str = Depends(user_dependency)):
        node = {
            "name": f"password-test-{secrets.token_hex(8)}",
            "address": input.address,
            "ssh_port": input.ssh_port,
            "ssh_user": input.ssh_user,
            "ssh_host_key": normalize_host_key(input.ssh_host_key),
            "ssh_auth_state": "pending",
        }
        authenticated, message = await asyncio.to_thread(password_test, node, input.password)
        audit_fn(username, "host_password_test", input.address, "succeeded" if authenticated else "failed")
        return {"authenticated": authenticated, "message": message}

    @router.post("/api/nodes/enroll", status_code=201)
    async def enroll_node(input: enrollment_model, username: str = Depends(user_dependency)):
        if input.install_controller_key and not enrollment_key():
            raise HTTPException(409, "Generate or import a controller-owned SSH key before enrolling this host")
        if input.auth_method == "controller_key" and not enrollment_key():
            raise HTTPException(409, "A controller-owned SSH key is required for key-based enrollment")
        host_key = normalize_host_key(input.ssh_host_key)
        requested_name = input.name.strip()
        temporary_name = requested_name or f"pending-{secrets.token_hex(8)}"
        try:
            with db_factory() as connection:
                if input.zone_id:
                    if not input.zone_cluster_id:
                        raise HTTPException(422, "Select the cluster that defines this host zone")
                    validate_zone_change(connection, -1, input.zone_cluster_id, input.zone_id)
                node = HostRepository.from_connection(connection).enroll_pending_in_connection(
                    connection,
                    input.model_dump(),
                    name=temporary_name,
                    host_key=host_key,
                )
        except Exception as error:
            if error.__class__.__name__ == "IntegrityError":
                raise HTTPException(409, "Node name already exists") from error
            raise
        node["enabled"] = bool(input.enabled)
        if input.auth_method == "password":
            run_id = launch_password_enrollment(
                node,
                input.password,
                input.install_controller_key,
                username,
                not requested_name,
            )
        else:
            run_id = launch_key_enrollment_probe(node, username, not requested_name)
        return {"id": node["id"], "run_id": run_id}

    @router.post("/api/nodes/{node_id}/controller-key")
    async def install_controller_key(node_id: int, input: key_install_model, username: str = Depends(user_dependency)):
        with db_factory() as connection:
            node = HostRepository.from_connection(connection).get(node_id)
            if node:
                require_no_conflict(connection, node_id)
        if not node:
            raise HTTPException(404, "Node not found")
        return {"run_id": launch_password_enrollment(node, input.password, True, username, False)}

    @router.put("/api/nodes/{node_id}")
    async def update_node(node_id: int, input: node_update_model, username: str = Depends(user_dependency)):
        with db_factory() as connection:
            repository = HostRepository.from_connection(connection)
            existing = repository.get(node_id)
            if not existing:
                raise HTTPException(404, "Node not found")
            require_no_conflict(connection, node_id)
            host_key = normalize_host_key(input.ssh_host_key) if input.ssh_host_key is not None else existing["ssh_host_key"]
            repository.update_in_connection(connection, node_id, input.model_dump(), host_key=host_key)
            if input.ssh_host_key is not None and host_key != existing["ssh_host_key"]:
                write_event_in_connection(
                    connection,
                    username,
                    "host_ssh_host_key_replaced" if host_key else "host_ssh_host_key_validation_disabled",
                    item_id=str(node_id),
                    detail=fingerprint(host_key) if host_key else "host key validation disabled",
                )
        return {"updated": True}

    @router.put("/api/nodes/{node_id}/zone")
    async def update_node_zone(node_id: int, input: zone_model, username: str = Depends(user_dependency)):
        assignment_ids: list[int] = []
        unchanged = False
        with db_factory() as connection:
            host_repository = HostRepository.from_connection(connection)
            node = host_repository.get(node_id)
            if not node:
                raise HTTPException(404, "Node not found")
            require_no_conflict(connection, node_id)
            validate_zone_change(connection, node_id, input.cluster_id, input.zone_id)
            previous = node["zone_id"]
            if previous == input.zone_id:
                unchanged = True
            else:
                workloads = WorkloadRepository.from_connection(connection)
                assignment_ids = workloads.active_elasticsearch_ids_for_node_in_connection(connection, node_id)
                host_repository.set_zone_in_connection(connection, node_id, input.zone_id)
                write_event_in_connection(
                    connection,
                    username,
                    "host_zone_updated",
                    item_id=str(node_id),
                    detail=f"{previous or 'unassigned'} -> {input.zone_id}",
                )
                if assignment_ids:
                    context = {
                        "node_id": node_id,
                        "previous_zone": previous,
                        "zone_id": input.zone_id,
                        "assignment_ids": assignment_ids,
                    }
                    run = start_run_in_connection(
                        connection,
                        RunDescriptor("host-zone-change", node["name"] + ":zone", context),
                    )
                    run_id = run.run_id
                    if not workloads.claim_operations_in_connection(connection, assignment_ids, run_id):
                        raise HTTPException(409, "A workload on this host is already part of an active operation")
                else:
                    run_id = None
        if unchanged:
            return {
                "updated": True,
                "run_id": completed_run(
                    "host-zone-change", node["name"] + ":zone", f"Host already uses {input.zone_id}.", None
                ),
            }
        if assignment_ids:
            inventory = inventory_for_run(run_id)
            asyncio.create_task(run_zone_change(run_id, node_id, previous, input.zone_id, inventory))
        else:
            run_id = completed_run(
                "host-zone-change",
                node["name"] + ":zone",
                f"Host zone set to {input.zone_id}.",
                {"node_id": node_id, "previous_zone": previous, "zone_id": input.zone_id},
            )
        return {"updated": True, "run_id": run_id}

    @router.post("/api/nodes/{node_id}/legacy-known-hosts/remove")
    async def remove_legacy_known_hosts_record(node_id: int, username: str = Depends(user_dependency)):
        with db_factory() as connection:
            repository = HostRepository.from_connection(connection)
            node = repository.get(node_id)
            if not node:
                raise HTTPException(404, "Node not found")
            require_no_conflict(connection, node_id)
            if node["ssh_auth_state"] != "legacy":
                raise HTTPException(409, "This host is not using legacy SSH host-key trust")
            repository.disable_legacy_known_hosts_in_connection(connection, node_id)
            write_event_in_connection(
                connection,
                username,
                "host_legacy_known_hosts_removed",
                item_id=str(node_id),
                detail="legacy host-key trust disabled for this host",
            )
        return {"updated": True, "legacy_known_hosts_disabled": True}

    @router.delete("/api/nodes/{node_id}/ssh-host-key")
    async def remove_host_key_record(node_id: int, username: str = Depends(user_dependency)):
        with db_factory() as connection:
            repository = HostRepository.from_connection(connection)
            node = repository.get(node_id)
            if not node:
                raise HTTPException(404, "Node not found")
            require_no_conflict(connection, node_id)
            host_key = node["ssh_host_key"]
            if not host_key:
                raise HTTPException(404, "No recorded SSH host key for this node")
            repository.clear_host_key_in_connection(connection, node_id)
            write_event_in_connection(
                connection,
                username,
                "host_ssh_host_key_removed",
                item_id=str(node_id),
                detail=fingerprint(host_key),
            )
        return {"updated": True}

    @router.delete("/api/nodes/{node_id}")
    async def delete_node(
        node_id: int,
        username: str = Depends(user_dependency),
        revoke_controller_key: bool = False,
        records_only: bool = False,
    ):
        with db_factory() as connection:
            repository = HostRepository.from_connection(connection)
            node = repository.get(node_id)
            if not node:
                raise HTTPException(404, "Node not found")
            require_no_conflict(connection, node_id)
            if has_membership(connection, node_id):
                raise HTTPException(409, "Remove this host from clusters before deleting its inventory record")
            if revoke_controller_key and records_only:
                raise HTTPException(422, "Choose either controller key revocation or records-only deletion")
            active, candidate = controller_key_rows()
            installed_key_id = node["ssh_key_id"] or node["candidate_key_id"]
            installed_key = next(
                (key for key in (active, candidate) if key and key["key_id"] == installed_key_id),
                None,
            )
            if revoke_controller_key:
                if not installed_key:
                    raise HTTPException(409, "This host has no controller-owned SSH key available for revocation")
                run_id = launch_key_revocation(node, installed_key, node_id)
                audit_fn(
                    username,
                    "host_controller_key_revocation",
                    str(node_id),
                    "key revocation requested before inventory deletion",
                )
                return {"run_id": run_id}
            if installed_key and not records_only:
                raise HTTPException(409, "Choose controller key revocation or explicitly confirm records-only deletion")
            if records_only:
                write_event_in_connection(
                    connection,
                    username,
                    "host_records_only_deletion",
                    item_id=str(node_id),
                    detail="controller key remains on host",
                )
            deleted = repository.delete_in_connection(connection, node_id)
        if not deleted:
            raise HTTPException(404, "Node not found")
        return Response(status_code=204)

    @router.post("/api/nodes/{node_id}/probe")
    async def probe(node_id: int, _: str = Depends(user_dependency)):
        node = HostRepository(db_factory).get(node_id)
        if not node or not node["enabled"]:
            raise HTTPException(404, "Enabled node not found")
        return {"run_id": launch_probe(node["name"])}

    return router


__all__ = [
    "build_router",
    "build_lifecycle_router",
    "build_batch_router",
    "build_inventory_router",
    "build_management_router",
]
