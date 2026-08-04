"""Cluster-scoped version selection and guarded-upgrade policy."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import HTTPException


class VersionUpgradeService:
    """Own version-response shaping and non-mutating upgrade safety checks."""

    def __init__(
        self,
        *,
        cluster_record: Callable[[Any, int], dict],
        available_versions: Callable[[list[dict], bool], list[str]],
        default_stack_version: str,
        version_key: Callable[[str | None], tuple[int, int, int] | None],
        membership_ready: Callable[[dict | None], bool],
        observation_is_fresh: Callable[[dict | None], bool],
        topology_elasticsearch_roles: set[str] | frozenset[str] | tuple[str, ...],
    ):
        self._cluster_record = cluster_record
        self._available_versions = available_versions
        self._default_stack_version = default_stack_version
        self._version_key = version_key
        self._membership_ready = membership_ready
        self._observation_is_fresh = observation_is_fresh
        self._topology_elasticsearch_roles = frozenset(topology_elasticsearch_roles)

    def details(self, connection: Any, cluster_id: int, *, include_candidates: bool = True) -> dict:
        cluster = self._cluster_record(connection, cluster_id)
        candidates: list[str] = []
        registry_error = ""
        if include_candidates:
            try:
                candidates = self._available_versions(
                    cluster["assignments"], cluster["log_monitoring"]["filebeat_enabled"]
                )
            except HTTPException as error:
                registry_error = str(error.detail)
        return {
            "cluster_id": cluster_id,
            "available_versions": candidates,
            "registry_error": registry_error,
            "assignments": [
                {
                    "assignment_id": assignment["id"],
                    "role": assignment["role"],
                    "node_name": assignment["node_name"],
                    "desired_version": assignment["image_version"] or self._default_stack_version,
                    "observation": assignment["observation"],
                }
                for assignment in cluster["assignments"]
            ],
        }

    def validate_target(self, cluster: dict, target_version: str, candidates: list[str] | None = None):
        target = self._version_key(target_version)
        if not target:
            raise HTTPException(422, "Choose a complete release version such as 8.19.0")
        available = candidates
        if available is None:
            available = self._available_versions(
                cluster["assignments"], cluster["log_monitoring"]["filebeat_enabled"]
            )
        if target_version not in available:
            raise HTTPException(422, "Choose a version available for every active component in this cluster")
        return target

    def preflight(self, cluster: dict, target_version: str, candidates: list[str] | None = None) -> bool:
        target = self.validate_target(cluster, target_version, candidates)
        if not cluster["assignments"]:
            raise HTTPException(422, "Assign workloads before requesting an upgrade")
        members = {member["node_id"]: member for member in cluster["members"]}
        versions: list[tuple[int, int, int]] = []
        for assignment in cluster["assignments"]:
            if not self._membership_ready(members.get(assignment["node_id"])):
                raise HTTPException(
                    422,
                    "Configure valid dedicated or shared data and user network bindings before upgrading this cluster",
                )
            observation = assignment["observation"]
            if not self._observation_is_fresh(observation) or not observation["running"] or observation["error"]:
                raise HTTPException(422, "Refresh running component versions successfully before upgrading")
            current = self._version_key(observation["version"])
            if not current:
                raise HTTPException(422, "A managed workload does not report a supported release version")
            if target <= current:
                raise HTTPException(422, "The selected version must be newer than every running component")
            if target[0] > current[0] + 1:
                raise HTTPException(422, "Upgrade one major version at a time")
            versions.append(current)
        elasticsearch = [
            assignment
            for assignment in cluster["assignments"]
            if assignment["role"] in self._topology_elasticsearch_roles
        ]
        if elasticsearch and sum(assignment["role"] == "master" for assignment in elasticsearch) < 3:
            raise HTTPException(422, "Safe Elasticsearch rolling upgrade requires three healthy master-eligible workloads")
        return any(target[0] > current[0] for current in versions)
