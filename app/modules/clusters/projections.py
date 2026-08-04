"""Cross-domain read projections assembled through public module repositories."""

from __future__ import annotations

import json
from typing import Any, Callable

from fastapi import HTTPException

from app.modules.hosts import HostRepository
from app.modules.versions import VersionRepository
from app.modules.workloads import WorkloadRepository

from .network import membership_ready
from .repository import ClusterRepository


class ClusterProjectionService:
    """Build the compatibility cluster payload without repository-level joins."""

    def __init__(self, connection):
        self._connection = connection
        self._clusters = ClusterRepository.from_connection(connection)
        self._hosts = HostRepository.from_connection(connection)
        self._workloads = WorkloadRepository.from_connection(connection)
        self._versions = VersionRepository.from_connection(connection)

    def record(
        self,
        cluster_id: int,
        *,
        default_theme_color: str,
        default_version: str,
        stored_role_ports: Callable[[str, dict], dict],
        stored_zoning: Callable[[str], Any],
        log_monitoring_config: Callable[[str], dict],
        stored_provider_profile: Callable[[dict], Any],
        provider_payload: Callable[[Any, str | None], dict],
        open_config: Callable[[str], dict],
        redacted_config: Callable[[dict], dict],
    ) -> dict:
        cluster = self._clusters.record_in_connection(self._connection, cluster_id)
        if not cluster:
            raise HTTPException(404, "Cluster not found")
        result = dict(cluster)
        result["ports"] = json.loads(result.pop("ports_json"))
        result["role_ports"] = stored_role_ports(result.pop("role_ports_json", ""), result["ports"])
        result["theme_color"] = (result.get("theme_color") or default_theme_color).upper()
        result["desired_version"] = result.get("desired_version") or default_version
        result["network_defaults"] = json.loads(result.pop("network_defaults_json", "{}") or "{}")
        result["elasticsearch_settings"] = json.loads(result.pop("elasticsearch_settings_json", "{}") or "{}")
        result["zoning"] = stored_zoning(result.pop("zoning_json", "{}")).model_dump()
        result["log_monitoring"] = log_monitoring_config(result.pop("observability_json", "{}"))
        profile = stored_provider_profile(result)
        result["provider"] = provider_payload(profile, result.pop("expected_cluster_uuid", None))
        for field in (
            "provider_type",
            "ownership_state",
            "maintenance_backend",
            "provider_capabilities_json",
            "provider_connection_json",
            "provider_revision",
        ):
            result.pop(field, None)

        zoning_observation = self._clusters.zoning_observation_record_in_connection(
            self._connection, cluster_id
        )
        result["zoning_status"] = (
            {
                **zoning_observation,
                "applied_zones": json.loads(zoning_observation["applied_zones_json"] or "[]"),
                "observed_zones": json.loads(zoning_observation["observed_zones_json"] or "{}"),
            }
            if zoning_observation
            else {
                "applied_mode": "disabled",
                "applied_zones": [],
                "observed_zones": {},
                "status": "pending" if result["zoning"]["mode"] != "disabled" else "disabled",
                "last_run_id": None,
                "observed_at": None,
                "last_error": "",
            }
        )
        result["zoning_status"].pop("applied_zones_json", None)
        result["zoning_status"].pop("observed_zones_json", None)

        memberships = self._clusters.memberships_in_connection(self._connection, cluster_id)
        hosts = self._hosts.records_for_ids_in_connection(
            self._connection, [int(member["node_id"]) for member in memberships]
        )
        result["members"] = [
            {
                **member,
                "name": hosts.get(int(member["node_id"]), {}).get("name", "unknown"),
                "address": hosts.get(int(member["node_id"]), {}).get("address", ""),
                "enabled": bool(hosts.get(int(member["node_id"]), {}).get("enabled")),
                "zone_id": hosts.get(int(member["node_id"]), {}).get("zone_id"),
                "network_ready": membership_ready(member),
            }
            for member in sorted(memberships, key=lambda value: hosts.get(int(value["node_id"]), {}).get("name", ""))
        ]

        assignments = self._workloads.active_for_cluster_in_connection(self._connection, cluster_id)
        assignment_ids = [int(assignment["id"]) for assignment in assignments]
        observations = self._versions.observations_for_assignments_in_connection(
            self._connection, assignment_ids
        )
        assignment_hosts = self._hosts.records_for_ids_in_connection(
            self._connection, [int(assignment["node_id"]) for assignment in assignments]
        )
        result["assignments"] = []
        for assignment in sorted(
            assignments,
            key=lambda value: (assignment_hosts.get(int(value["node_id"]), {}).get("name", ""), value["role"]),
        ):
            observation = observations.get(int(assignment["id"]))
            result["assignments"].append(
                {
                    "id": assignment["id"],
                    "cluster_id": assignment["cluster_id"],
                    "node_id": assignment["node_id"],
                    "node_name": assignment_hosts.get(int(assignment["node_id"]), {}).get("name", "unknown"),
                    "role": assignment["role"],
                    "state": assignment["state"],
                    "revision": assignment["revision"],
                    "image_version": assignment["image_version"],
                    "config": redacted_config(open_config(assignment["config_json"])),
                    "observation": (
                        {
                            "image": observation["image"],
                            "digest": observation["digest"],
                            "version": observation["version"],
                            "running": bool(observation["running"]),
                            "cached": bool(observation["cached"]),
                            "observed_at": observation["observed_at"],
                            "error": observation["error"],
                        }
                        if observation and observation["observed_at"]
                        else None
                    ),
                    "filebeat": (
                        {
                            "state": observation["filebeat_state"],
                            "observed_at": observation["filebeat_observed_at"],
                            "error": observation["filebeat_error"],
                        }
                        if observation and observation["filebeat_observed_at"]
                        else {"state": "disabled", "error": ""}
                    ),
                }
            )
        return result
