"""Cluster settings application service.

The service owns the durable cluster settings update while request routing and
legacy application helpers remain compatibility adapters during extraction.
"""

from __future__ import annotations

from typing import Any, Callable

from .repository import ClusterRepository


class ClusterSettingsService:
    """Apply desired Elasticsearch settings without coupling to FastAPI."""

    def __init__(
        self,
        *,
        db_factory: Callable,
        cluster_record: Callable,
        require_no_maintenance_conflict: Callable,
        require_cluster_capability: Callable,
        settings_capability: Any,
        open_config: Callable[[str], dict],
        completed_run: Callable[[str, str, str, dict | None], int],
        launch_settings: Callable[[dict, dict, dict, Any], int],
    ):
        self._db = db_factory
        self._cluster_record = cluster_record
        self._require_no_maintenance_conflict = require_no_maintenance_conflict
        self._require_cluster_capability = require_cluster_capability
        self._settings_capability = settings_capability
        self._open_config = open_config
        self._completed_run = completed_run
        self._launch_settings = launch_settings

    def get(self, cluster_id: int) -> dict:
        with self._db() as connection:
            cluster = self._cluster_record(connection, cluster_id)
        return {
            "cluster_id": cluster_id,
            "theme_color": cluster["theme_color"],
            "desired_version": cluster["desired_version"],
            "network_defaults": cluster["network_defaults"],
            "elasticsearch_settings": cluster["elasticsearch_settings"],
        }

    async def update(self, cluster_id: int, settings: Any, username: str) -> dict:
        del username  # Audit/run identity is retained by the launching caller.
        with self._db() as connection:
            cluster = self._cluster_record(connection, cluster_id)
            repository = ClusterRepository.from_connection(connection)
            self._require_no_maintenance_conflict(connection, cluster_id=cluster_id)
            self._require_cluster_capability(connection, cluster_id, self._settings_capability)
            repository.update_elasticsearch_settings_in_connection(
                connection, cluster_id, settings.model_dump_json()
            )
            master = next((item for item in cluster["assignments"] if item["role"] == "master"), None)
            if master:
                member = next(item for item in cluster["members"] if item["node_id"] == master["node_id"])
                credentials = self._open_config(
                    repository.secrets_json_row_in_connection(connection, cluster_id)["secrets_json"]
                )
        if not master:
            return {
                "updated": True,
                "run_id": self._completed_run(
                    "cluster-settings",
                    cluster["name"],
                    "Stored settings; no master is assigned",
                ),
            }
        return {
            "updated": True,
            "run_id": self._launch_settings(cluster, master, member, settings, credentials),
        }


__all__ = ["ClusterSettingsService"]
