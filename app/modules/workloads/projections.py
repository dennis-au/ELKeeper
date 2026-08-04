"""Compatibility workload projections assembled via public repositories."""

from __future__ import annotations

from fastapi import HTTPException

from app.modules.clusters import ClusterRepository
from app.modules.hosts import HostRepository

from .repository import WorkloadRepository


class WorkloadProjectionService:
    """Resolve the cross-domain assignment shape used by reconciliation code."""

    def __init__(self, connection):
        self._connection = connection
        self._clusters = ClusterRepository.from_connection(connection)
        self._hosts = HostRepository.from_connection(connection)
        self._workloads = WorkloadRepository.from_connection(connection)

    def assignment_record(self, assignment_id: int) -> dict:
        assignment = self._workloads.record_in_connection(self._connection, assignment_id)
        if not assignment:
            raise HTTPException(404, "Assignment not found")
        cluster = self._clusters.record_in_connection(self._connection, int(assignment["cluster_id"]))
        host = self._hosts.get(int(assignment["node_id"]))
        membership = self._clusters.membership_in_connection(
            self._connection, int(assignment["cluster_id"]), int(assignment["node_id"])
        )
        if not cluster or not host or not membership:
            raise HTTPException(409, "Assignment inventory is incomplete and must be repaired before reconciliation")
        return {
            **assignment,
            "cluster_name": cluster["name"],
            "slug": cluster["slug"],
            "ports_json": cluster["ports_json"],
            "role_ports_json": cluster["role_ports_json"],
            "secrets_json": cluster["secrets_json"],
            "node_name": host["name"],
            "node_address": host["address"],
            "zone_id": host.get("zone_id"),
            "network_mode": membership["network_mode"],
            "data_interface": membership["data_interface"],
            "data_address": membership["data_address"],
            "user_interface": membership["user_interface"],
            "user_address": membership["user_address"],
        }

    def active_change_set_records(self, cluster_id: int) -> list[dict]:
        """Return active assignments with the host/membership fields used by staging."""

        assignments = self._workloads.active_for_cluster_in_connection(self._connection, cluster_id)
        hosts = self._hosts.records_for_ids_in_connection(
            self._connection, [int(item["node_id"]) for item in assignments]
        )
        members = {
            int(item["node_id"]): item
            for item in self._clusters.memberships_in_connection(self._connection, cluster_id)
        }
        records = []
        for assignment in assignments:
            host = hosts.get(int(assignment["node_id"]))
            member = members.get(int(assignment["node_id"]))
            if not host or not member:
                continue
            records.append({
                **assignment,
                "node_name": host["name"],
                "enabled": host["enabled"],
                "zone_id": host.get("zone_id"),
                "network_mode": member["network_mode"],
                "data_interface": member["data_interface"],
                "data_address": member["data_address"],
                "user_interface": member["user_interface"],
                "user_address": member["user_address"],
            })
        return records

    def member_record(self, cluster_id: int, node_id: int) -> dict | None:
        membership = self._clusters.membership_in_connection(self._connection, cluster_id, node_id)
        host = self._hosts.get(node_id)
        if not membership or not host:
            return None
        return {
            **membership,
            "node_name": host["name"],
            "enabled": host["enabled"],
            "zone_id": host.get("zone_id"),
        }
