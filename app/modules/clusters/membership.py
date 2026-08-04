"""Cluster membership validation and persistence adapters.

The application assembly supplies repository classes so this public cluster
contract stays independent of private host and workload implementations.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException

from .contracts import ZoningConfig
from .network import membership_ready


class MembershipOperations:
    """Own membership readiness, zone validation, and repository delegation."""

    def __init__(self, *, cluster_repository: type, host_repository: type, workload_repository: type) -> None:
        self._clusters = cluster_repository
        self._hosts = host_repository
        self._workloads = workload_repository

    @staticmethod
    def stored_zoning(value: str | None) -> ZoningConfig:
        try:
            import json

            return ZoningConfig.model_validate(json.loads(value or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return ZoningConfig()

    @staticmethod
    def require_cluster_host_zone(cluster: dict, member: Any) -> None:
        zoning = cluster["zoning"]
        zone_id = member["zone_id"] if member and "zone_id" in member.keys() else None
        if zoning["mode"] == "disabled":
            return
        if not zone_id:
            raise HTTPException(422, "Select a cluster-defined host zone before adding or applying Elasticsearch workloads")
        if zone_id not in zoning["zones"]:
            raise HTTPException(422, f"Host zone {zone_id} is not defined by this cluster")

    @staticmethod
    def require_ready(member: Any) -> None:
        if not membership_ready(member):
            raise HTTPException(422, "Configure valid dedicated or shared data and user network bindings before applying or reconciling this workload")

    def node_record(self, connection: Any, node_id: int):
        return self._hosts.from_connection(connection).get(node_id)

    def insert(self, connection: Any, cluster_id: int, membership: Any) -> None:
        self._clusters.from_connection(connection).insert_membership_in_connection(connection, cluster_id, membership)

    def update(self, connection: Any, cluster_id: int, node_id: int, membership: Any) -> bool:
        return self._clusters.from_connection(connection).update_membership_in_connection(connection, cluster_id, node_id, membership)

    def has_assignments(self, connection: Any, cluster_id: int, node_id: int) -> bool:
        return self._workloads.from_connection(connection).has_assignments_for_member_in_connection(connection, cluster_id, node_id)

    def delete(self, connection: Any, cluster_id: int, node_id: int) -> None:
        self._clusters.from_connection(connection).delete_membership_in_connection(connection, cluster_id, node_id)


__all__ = ["MembershipOperations"]
