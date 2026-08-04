"""Validation of staged cluster workload changes.

All cluster, repository, and configuration access is injected by assembly so
the workload module owns the policy without importing another module's private
implementation.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import HTTPException


class WorkloadChangeValidator:
    """Validate a complete staged workload change set before it is persisted."""

    def __init__(
        self,
        *,
        cluster_record: Callable,
        active_operation: Callable,
        active_assignments: Callable,
        validate_config: Callable,
        recommended_version: Callable,
        default_version: str,
        projection_factory: Callable,
        require_ready_membership: Callable,
        require_cluster_host_zone: Callable,
        elasticsearch_roles: frozenset[str],
        conflict_message: Callable,
        open_config: Callable,
        validate_final_ports: Callable,
    ) -> None:
        self._cluster_record = cluster_record
        self._active_operation = active_operation
        self._active_assignments = active_assignments
        self._validate_config = validate_config
        self._recommended_version = recommended_version
        self._default_version = default_version
        self._projection_factory = projection_factory
        self._require_ready = require_ready_membership
        self._require_zone = require_cluster_host_zone
        self._elasticsearch_roles = elasticsearch_roles
        self._conflict_message = conflict_message
        self._open_config = open_config
        self._validate_final_ports = validate_final_ports

    def validate(self, connection: Any, cluster_id: int, change_set: Any) -> tuple[dict, list[dict]]:
        cluster = self._cluster_record(connection, cluster_id)
        if self._active_operation(connection, cluster["name"]):
            raise HTTPException(409, "Wait for the active cluster operation to finish")
        active = self._active_assignments(connection, cluster_id)
        active_by_id = {row["id"]: row for row in active}
        planned: list[dict] = []
        changed_ids: set[int] = set()
        projections = self._projection_factory(connection)

        for change in change_set.changes:
            item = change.model_dump()
            if item["kind"] == "create":
                self._validate_config(item["role"], item["config"])
                item["image_version"] = (
                    item["image_version"]
                    or self._recommended_version(active, [])
                    or cluster["desired_version"]
                    or self._default_version
                )
                member = projections.member_record(cluster_id, item["node_id"])
                if not member:
                    raise HTTPException(422, "Add the host to this cluster first")
                if not member["enabled"]:
                    raise HTTPException(422, "Enable the host before applying a role")
                self._require_ready(member)
                if item["role"] in self._elasticsearch_roles:
                    self._require_zone(cluster, member)
                conflict = self._conflict_message(connection, cluster_id, item["node_id"], item["role"])
                if conflict:
                    raise HTTPException(409, conflict)
                if any(row["node_id"] == item["node_id"] and row["role"] == item["role"] for row in active):
                    raise HTTPException(409, "This role is already managed on the selected host")
                planned.append({**item, "node_name": member["node_name"]})
                continue

            row = active_by_id.get(item["assignment_id"])
            if not row:
                raise HTTPException(404, "Managed workload not found")
            if row["operation_run_id"]:
                raise HTTPException(409, "This workload is already part of an active change set")
            if row["revision"] != item["expected_revision"]:
                raise HTTPException(409, "This workload changed since it was staged; refresh and stage it again")
            if row["id"] in changed_ids:
                raise HTTPException(422, "A workload can only appear once in a pending change set")
            changed_ids.add(row["id"])
            if item["kind"] == "resources":
                previous_config = self._open_config(row["config_json"])
                next_config = {**previous_config, **item["config"]}
                self._validate_config(row["role"], next_config)
                if not row["enabled"]:
                    raise HTTPException(422, "Enable the host before applying a role")
                self._require_ready(row)
                if row["role"] in self._elasticsearch_roles:
                    self._require_zone(cluster, row)
                planned.append(
                    {
                        **item,
                        "node_id": row["node_id"],
                        "node_name": row["node_name"],
                        "role": row["role"],
                        "config": next_config,
                        "previous_config": previous_config,
                    }
                )
            else:
                planned.append({**item, "node_id": row["node_id"], "node_name": row["node_name"], "role": row["role"]})

        final_assignments = [
            row
            for row in active
            if row["id"] not in {item["assignment_id"] for item in planned if item["kind"] == "detach"}
        ]
        final_assignments.extend(
            {"node_id": item["node_id"], "node_name": item["node_name"], "role": item["role"]}
            for item in planned
            if item["kind"] == "create"
        )
        self._validate_final_ports(cluster, final_assignments)
        active_masters = [row for row in active if row["role"] == "master"]
        initial_master = active_masters[0] if active_masters else None
        detached = {item["assignment_id"] for item in planned if item["kind"] == "detach"}
        if initial_master and initial_master["id"] in detached and final_assignments:
            raise HTTPException(409, "Detach the dependent cluster roles before removing the initial master")
        final_roles = {item["role"] for item in final_assignments}
        if any(item["kind"] == "create" and item["role"] != "master" for item in planned) and "master" not in final_roles:
            raise HTTPException(422, "Deploy a master before this workload")
        if "fleet-server" in final_roles and "kibana" not in final_roles:
            raise HTTPException(422, "Deploy Kibana before Fleet Server")
        if "elastic-agent" in final_roles and "fleet-server" not in final_roles:
            raise HTTPException(422, "Deploy Fleet Server before Elastic Agent")
        return cluster, planned


__all__ = ["WorkloadChangeValidator"]
