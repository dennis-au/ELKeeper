"""Cluster-domain application services."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Callable

from fastapi import HTTPException

from app.modules.platform import write_event_in_connection
from app.modules.workloads import WorkloadRepository

from .repository import ClusterRepository


class ClusterService:
    def __init__(self, db_factory: Callable):
        self.repository = ClusterRepository(db_factory)

    def ids(self) -> list[int]:
        return self.repository.ids()

    def exists(self, cluster_id: int) -> bool:
        return self.repository.exists(cluster_id)


class ClusterLifecycleService:
    """Own cluster lifecycle persistence while accepting compatibility policies.

    Validation and maintenance capability checks are injected because their
    reconciliation workers are still being extracted. The service owns every
    cluster table write and keeps route payloads stable during that migration.
    """

    def __init__(
        self,
        db_factory: Callable,
        *,
        slugify: Callable[[str], str],
        seal_config: Callable[[str], str],
        token_factory: Callable[[int], str],
        log_monitoring_config: Callable[..., dict],
        palette: tuple[str, ...],
        stored_provider_profile: Callable[[dict], Any],
        provider_payload: Callable[[Any, str | None], dict],
        require_no_maintenance_conflict: Callable[..., None],
        require_cluster_capability: Callable[..., None],
        cluster_settings_capability: Any,
        profile_conflict: Callable[[Any, int, dict], str | None],
        validate_zoning_catalog_update: Callable[[Any, int, Any], None],
    ):
        self._db = db_factory
        self._slugify = slugify
        self._seal_config = seal_config
        self._token_factory = token_factory
        self._log_monitoring_config = log_monitoring_config
        self._palette = palette
        self._stored_provider_profile = stored_provider_profile
        self._provider_payload = provider_payload
        self._require_no_maintenance_conflict = require_no_maintenance_conflict
        self._require_cluster_capability = require_cluster_capability
        self._cluster_settings_capability = cluster_settings_capability
        self._profile_conflict = profile_conflict
        self._validate_zoning_catalog_update = validate_zoning_catalog_update

    @staticmethod
    def _values(input: Any, *, slug: str, color: str, secrets_json: str | None = None,
                observability_json: str | None = None) -> dict[str, str]:
        values = {
            "name": input.name,
            "slug": slug,
            "ports_json": input.ports.model_dump_json(),
            "role_ports_json": json.dumps(input.role_ports.model_dump(by_alias=True), sort_keys=True),
            "theme_color": color,
            "desired_version": input.desired_version,
            "network_defaults_json": input.network_defaults.model_dump_json(),
            "elasticsearch_settings_json": input.elasticsearch_settings.model_dump_json(),
            "zoning_json": input.zoning.model_dump_json(),
        }
        if secrets_json is not None:
            values["secrets_json"] = secrets_json
        if observability_json is not None:
            values["observability_json"] = observability_json
        return values

    def create(self, input: Any) -> dict[str, int]:
        try:
            with self._db() as connection:
                repository = ClusterRepository.from_connection(connection)
                color = (input.theme_color or repository.next_theme_color_in_connection(connection, self._palette)).upper()
                encrypted_secrets = self._seal_config(json.dumps({
                    "elastic_password": self._token_factory(24),
                    "kibana_password": self._token_factory(24),
                    "monitoring_password": self._token_factory(24),
                    "filebeat_password": self._token_factory(24),
                }))
                cluster_id = repository.create_in_connection(
                    connection,
                    self._values(
                        input,
                        slug=self._slugify(input.name),
                        color=color,
                        secrets_json=encrypted_secrets,
                        observability_json=json.dumps(self._log_monitoring_config("", default_enabled=True), sort_keys=True),
                    ),
                )
            return {"id": cluster_id}
        except sqlite3.IntegrityError as error:
            raise HTTPException(409, "Cluster name already exists") from error

    def get_provider(self, cluster_id: int) -> dict:
        with self._db() as connection:
            record = ClusterRepository.from_connection(connection).record_in_connection(connection, cluster_id)
        if not record:
            raise HTTPException(404, "Cluster not found")
        return self._provider_payload(
            self._stored_provider_profile(record), record.get("expected_cluster_uuid")
        )

    def update_provider(self, cluster_id: int, input: Any, username: str) -> dict:
        with self._db() as connection:
            clusters = ClusterRepository.from_connection(connection)
            workloads = WorkloadRepository.from_connection(connection)
            record = clusters.record_in_connection(connection, cluster_id)
            if not record:
                raise HTTPException(404, "Cluster not found")
            self._require_no_maintenance_conflict(connection, cluster_id=cluster_id)
            current = self._stored_provider_profile(record)
            if current.revision != input.expected_revision:
                raise HTTPException(409, "Cluster provider metadata changed; reload before saving")
            if (
                current.provider_type != input.provider_type
                or current.maintenance_backend != input.maintenance_backend
            ) and workloads.has_assignments_for_cluster_in_connection(connection, cluster_id):
                raise HTTPException(409, "Remove managed assignments before changing provider type or backend")
            profile_type = type(current)
            profile = profile_type(
                provider_type=input.provider_type,
                ownership_state=input.ownership_state,
                maintenance_backend=input.maintenance_backend,
                capability_overrides=input.capability_overrides,
                connection_references=input.connection_references,
                revision=current.revision + 1,
            )
            if not clusters.update_provider_in_connection(
                connection,
                cluster_id,
                input.expected_revision,
                {
                    "provider_type": profile.provider_type.value,
                    "ownership_state": profile.ownership_state.value,
                    "maintenance_backend": profile.maintenance_backend.value,
                    "provider_capabilities_json": json.dumps(dict(profile.capability_overrides), sort_keys=True),
                    "provider_connection_json": json.dumps(dict(profile.connection_references), sort_keys=True),
                    "expected_cluster_uuid": input.expected_cluster_uuid,
                },
            ):
                raise HTTPException(409, "Cluster provider metadata changed; reload before saving")
            write_event_in_connection(
                connection,
                username,
                "cluster_provider_updated",
                cluster_id=cluster_id,
                item_id=str(cluster_id),
                detail=f"{profile.provider_type.value}:{profile.ownership_state.value}:revision={profile.revision}",
            )
        return self._provider_payload(profile, input.expected_cluster_uuid)

    def update(self, cluster_id: int, input: Any) -> dict[str, bool]:
        try:
            with self._db() as connection:
                clusters = ClusterRepository.from_connection(connection)
                record = clusters.record_in_connection(connection, cluster_id)
                if not record:
                    raise HTTPException(404, "Cluster not found")
                self._require_no_maintenance_conflict(connection, cluster_id=cluster_id)
                self._require_cluster_capability(
                    connection, cluster_id, self._cluster_settings_capability
                )
                conflict = self._profile_conflict(
                    connection, cluster_id, input.role_ports.model_dump(by_alias=True)
                )
                if conflict:
                    raise HTTPException(409, conflict)
                self._validate_zoning_catalog_update(connection, cluster_id, input.zoning)
                color = (input.theme_color or clusters.next_theme_color_in_connection(connection, self._palette)).upper()
                if not clusters.update_in_connection(
                    connection,
                    cluster_id,
                    # The slug owns remote unit names, certificate paths, and
                    # persistent data markers. It is an immutable namespace;
                    # the display name can change without renaming a live stack.
                    self._values(input, slug=record["slug"], color=color),
                ):
                    raise HTTPException(404, "Cluster not found")
            return {"updated": True}
        except sqlite3.IntegrityError as error:
            raise HTTPException(409, "Cluster name already exists") from error

    def delete(self, cluster_id: int, *, invalidate_cluster_ca: Callable[[int], None]) -> None:
        with self._db() as connection:
            clusters = ClusterRepository.from_connection(connection)
            workloads = WorkloadRepository.from_connection(connection)
            self._require_no_maintenance_conflict(connection, cluster_id=cluster_id)
            if workloads.has_assignments_for_cluster_in_connection(connection, cluster_id):
                raise HTTPException(409, "Detach or purge all roles before deleting the cluster")
            if not clusters.delete_in_connection(connection, cluster_id):
                raise HTTPException(404, "Cluster not found")
        invalidate_cluster_ca(cluster_id)
