"""Cluster-qualified workload payload assembly."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping

from fastapi import HTTPException


class WorkloadPayloadService:
    """Build Ansible payloads from public cluster/host/workload projections."""

    def __init__(
        self,
        *,
        cluster_repository,
        workload_repository,
        host_repository,
        cluster_record: Callable,
        open_config: Callable[[str], dict],
        stored_role_ports: Callable[[str, dict], dict],
        memory_mebibytes: Callable[[str], int],
        default_stack_version: str,
        elasticsearch_roles: set[str],
        require_ready_membership: Callable[[Mapping], None],
        require_cluster_host_zone: Callable[[Mapping, Mapping], None],
    ):
        self._clusters = cluster_repository
        self._workloads = workload_repository
        self._hosts = host_repository
        self._cluster_record = cluster_record
        self._open_config = open_config
        self._stored_role_ports = stored_role_ports
        self._memory_mebibytes = memory_mebibytes
        self._default_version = default_stack_version
        self._elasticsearch_roles = elasticsearch_roles
        self._require_ready = require_ready_membership
        self._require_zone = require_cluster_host_zone

    @staticmethod
    def _value(row: Mapping, key: str, default=None):
        try:
            return row[key]
        except (KeyError, IndexError, TypeError):
            return default

    def _assignment_projection(self, connection, cluster: Mapping, assignment: Mapping) -> dict:
        node = self._hosts.from_connection(connection).get(int(assignment["node_id"]))
        membership = self._clusters.from_connection(connection).membership_in_connection(
            connection, int(cluster["id"]), int(assignment["node_id"])
        ) or {}
        return {
            "id": int(assignment["id"]),
            "node_id": int(assignment["node_id"]),
            "config_json": assignment.get("config_json") or "{}",
            "node_name": (node or {}).get("name", ""),
            "node_address": (node or {}).get("address", ""),
            "zone_id": (node or {}).get("zone_id"),
            **membership,
        }

    def build(
        self,
        connection,
        row: Mapping,
        desired_state: str = "present",
        batch_assignment_ids=(),
        config_overrides: Mapping[int, Mapping] | None = None,
    ) -> dict:
        cluster_id = int(row["cluster_id"])
        cluster_details = self._cluster_record(connection, cluster_id)
        membership = self._clusters.from_connection(connection).membership_in_connection(
            connection, cluster_id, int(row["node_id"])
        ) or {}
        current = {**dict(row), **membership}
        if desired_state != "purge":
            self._require_ready(current)
            if row["role"] in self._elasticsearch_roles:
                self._require_zone(cluster_details, current)
        config_overrides = config_overrides or {}
        config = dict(config_overrides.get(int(row["id"]), self._open_config(row["config_json"])))
        if row["role"] in self._elasticsearch_roles and not str(config.get("jvm_heap", "")).strip():
            config["jvm_heap"] = f"{max(1024, self._memory_mebibytes(str(config['memory'])) // 2)}m"
        if row["role"] == "kibana" and str(config.get("node_heap", "")).strip():
            config["node_heap_mib"] = self._memory_mebibytes(str(config["node_heap"]))

        assignments = self._workloads.from_connection(connection).payload_assignments_in_connection(
            connection, cluster_id, included_ids=tuple(int(value) for value in batch_assignment_ids)
        )
        projected = [self._assignment_projection(connection, cluster_details, item) for item in assignments]
        master_rows = [
            item for item, source in zip(projected, assignments)
            if source["role"] == "master"
        ]
        bootstrap = master_rows[0] if master_rows else None
        if desired_state != "purge" and row["role"] != "master" and not bootstrap:
            raise HTTPException(422, "Deploy a master before this workload")
        role_ports = self._stored_role_ports(row["role_ports_json"], json.loads(row["ports_json"]))
        masters = [
            {
                "assignment_id": master["id"], "node_id": master["node_id"], "node_name": master["node_name"],
                "node_address": master["node_address"], "network_mode": master.get("network_mode"),
                "data_address": master.get("data_address"), "user_address": master.get("user_address"),
                "zone_id": master.get("zone_id"),
                "workload": f"ecp-{row['slug']}-master-{master['node_id']}", "ports": role_ports["master"],
            }
            for master in master_rows
        ]
        if bootstrap:
            if desired_state != "purge":
                self._require_ready(bootstrap)
            bootstrap_config = config_overrides.get(bootstrap["id"], self._open_config(bootstrap["config_json"]))
            bootstrap_data = {**masters[0], "storage_path": bootstrap_config["storage_path"]}
        else:
            bootstrap_data = None

        services = {}
        for service_role in ("kibana", "fleet-server"):
            service = next(
                (item for item, source in zip(projected, assignments) if source["role"] == service_role),
                None,
            )
            if service:
                if desired_state != "purge":
                    self._require_ready(service)
                services[service_role] = {
                    "assignment_id": service["id"], "node_id": service["node_id"], "node_name": service["node_name"],
                    "node_address": service["node_address"], "network_mode": service.get("network_mode"),
                    "data_address": service.get("data_address"), "user_address": service.get("user_address"),
                    "zone_id": service.get("zone_id"),
                    "workload": f"ecp-{row['slug']}-{service_role}-{service['node_id']}", "ports": role_ports[service_role],
                }
        if desired_state != "purge" and row["role"] == "fleet-server" and "kibana" not in services:
            raise HTTPException(422, "Deploy Kibana before Fleet Server")
        if desired_state != "purge" and row["role"] == "elastic-agent" and "fleet-server" not in services:
            raise HTTPException(422, "Deploy Fleet Server before Elastic Agent")
        return {
            "cluster": {
                "id": row["cluster_id"], "name": row["cluster_name"], "slug": row["slug"],
                "ports": json.loads(row["ports_json"]), "role_ports": role_ports, "zoning": cluster_details["zoning"],
            },
            "assignment": {
                "id": row["id"], "role": row["role"], "config": config,
                "image_version": row["image_version"] or self._default_version, "ports": role_ports[row["role"]],
            },
            "membership": {
                "node_id": row["node_id"], "network_mode": current.get("network_mode"),
                "data_interface": current.get("data_interface"), "data_address": current.get("data_address"),
                "user_interface": current.get("user_interface"), "user_address": current.get("user_address"),
                "zone_id": current.get("zone_id"),
            },
            "bootstrap": bootstrap_data, "masters": masters, "services": services,
            "credentials": self._open_config(row["secrets_json"]), "desired_state": desired_state,
        }


__all__ = ["WorkloadPayloadService"]
