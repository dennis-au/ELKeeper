"""Cluster-owned mutation policy and cross-cluster safety checks."""

from __future__ import annotations

import json
from collections.abc import Callable

from fastapi import HTTPException


class ClusterPolicyService:
    """Validate cluster zoning, provider capability, and port conflicts.

    The service owns policy decisions while repositories remain responsible for
    table access.  Callbacks keep legacy DTO/model types out of the cluster
    module and preserve the existing application-level compatibility seams.
    """

    def __init__(
        self,
        *,
        cluster_repository,
        workload_repository,
        host_repository,
        stored_zoning: Callable,
        role_port_values: Callable,
        stored_role_ports: Callable,
        role_specs: dict,
        active_cluster_operation: Callable,
        provider_profile_from_record: Callable,
        require_capability: Callable,
    ):
        self._clusters = cluster_repository
        self._workloads = workload_repository
        self._hosts = host_repository
        self._stored_zoning = stored_zoning
        self._role_port_values = role_port_values
        self._stored_role_ports = stored_role_ports
        self._role_specs = role_specs
        self._active_operation = active_cluster_operation
        self._provider_profile = provider_profile_from_record
        self._require_capability = require_capability

    def validate_zoning_catalog_update(self, connection, cluster_id: int, zoning) -> None:
        if zoning.mode == "disabled":
            return
        rows = self._clusters.from_connection(connection).memberships_in_connection(connection, cluster_id)
        used = sorted(
            {
                host.get("zone_id")
                for row in rows
                if (host := self._hosts.from_connection(connection).get(row["node_id"])) and host.get("zone_id")
            }
        )
        missing = [zone for zone in used if zone not in zoning.zones]
        if missing:
            raise HTTPException(409, "Reassign hosts before removing in-use zones: " + ", ".join(missing))

    def validate_host_zone_change(self, connection, node_id: int, cluster_id: int, zone_id: str) -> None:
        selected = self._clusters.from_connection(connection).record_in_connection(connection, cluster_id)
        if not selected:
            raise HTTPException(404, "Cluster not found")
        if zone_id not in self._stored_zoning(selected.get("zoning_json")).zones:
            raise HTTPException(422, f"Zone {zone_id} is not defined by the selected cluster")
        memberships = self._clusters.from_connection(connection).memberships_for_node_in_connection(connection, node_id)
        incompatible = []
        for membership in memberships:
            cluster = self._clusters.from_connection(connection).record_in_connection(connection, int(membership["cluster_id"]))
            if not cluster:
                continue
            zoning = self._stored_zoning(cluster.get("zoning_json"))
            if zoning.mode != "disabled" and zone_id not in zoning.zones:
                incompatible.append(cluster["name"])
        if incompatible:
            raise HTTPException(409, "The selected zone is not defined by associated clusters: " + ", ".join(incompatible))
        for membership in memberships:
            cluster = self._clusters.from_connection(connection).record_in_connection(connection, int(membership["cluster_id"]))
            if not cluster:
                continue
            if self._active_operation(connection, cluster["name"]):
                raise HTTPException(409, f"Wait for the active {cluster['name']} operation to finish")
            zoning = self._stored_zoning(cluster.get("zoning_json"))
            if zoning.mode != "forced_awareness":
                continue
            assignments = self._workloads.from_connection(connection).active_for_cluster_in_connection(connection, int(cluster["id"]))
            assignments = [item for item in assignments if item["role"] in {"hot", "warm"}]
            for role in ("hot", "warm"):
                role_rows = [item for item in assignments if item["role"] == role]
                if not role_rows:
                    continue
                zones = set()
                for item in role_rows:
                    if item["node_id"] == node_id:
                        zones.add(zone_id)
                    else:
                        host = self._hosts.from_connection(connection).get(item["node_id"])
                        if host and host.get("zone_id"):
                            zones.add(host["zone_id"])
                missing = [zone for zone in zoning.zones if zone not in zones]
                if missing:
                    label = self._role_specs[role]["label"]
                    raise HTTPException(422, f"Changing this host would leave {label} without forced zones: {', '.join(missing)}")

    def profile_conflict(self, connection, cluster_id: int, role_ports: dict) -> str | None:
        rows = self._workloads.from_connection(connection).active_or_applying_in_connection(connection)
        records = {int(item["id"]): item for item in rows}
        clusters = {int(item["cluster_id"]): self._clusters.from_connection(connection).record_in_connection(connection, int(item["cluster_id"])) for item in rows}
        for left in rows:
            left_cluster = clusters.get(int(left["cluster_id"]))
            if not left_cluster:
                continue
            left_ports = role_ports if left["cluster_id"] == cluster_id else self._stored_role_ports(
                left_cluster["role_ports_json"], json.loads(left_cluster["ports_json"])
            )
            for right in rows:
                if left["node_id"] != right["node_id"] or right["cluster_id"] == left["cluster_id"]:
                    continue
                right_cluster = clusters.get(int(right["cluster_id"]))
                if not right_cluster:
                    continue
                right_ports = role_ports if right["cluster_id"] == cluster_id else self._stored_role_ports(
                    right_cluster["role_ports_json"], json.loads(right_cluster["ports_json"])
                )
                if set(self._role_port_values(left_ports, left["role"])).intersection(
                    self._role_port_values(right_ports, right["role"])
                ):
                    return f"Port profile conflicts with {right_cluster['name']} on a shared host"
        return None

    def require_capability(self, connection, cluster_id: int, capability):
        record = self._clusters.from_connection(connection).record_in_connection(connection, cluster_id)
        if not record:
            raise HTTPException(404, "Cluster not found")
        profile = self._provider_profile(record)
        try:
            self._require_capability(profile, capability)
        except PermissionError as error:
            raise HTTPException(409, str(error)) from error
        return profile


__all__ = ["ClusterPolicyService"]
