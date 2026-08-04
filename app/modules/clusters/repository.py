"""Persistence boundary for cluster inventory and memberships.

Complex compatibility serialization remains in the application assembly for
now; this repository owns the basic cluster row lookup boundary.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
from typing import Callable

from app.modules.platform import SchemaIntrospection


class ClusterRepository:
    def __init__(self, db_factory: Callable | None = None, *, connection=None):
        if db_factory is None and connection is None:
            raise ValueError("ClusterRepository requires a database factory or connection")
        if db_factory is not None and connection is not None:
            raise ValueError("Provide either a database factory or connection, not both")
        self._db = db_factory
        self._connection = connection

    @classmethod
    def from_connection(cls, connection):
        return cls(connection=connection)

    @contextmanager
    def _connection_scope(self):
        if self._connection is not None:
            yield self._connection
            return
        assert self._db is not None
        with self._db() as connection:
            yield connection

    def ids(self) -> list[int]:
        with self._connection_scope() as connection:
            return [int(row["id"]) for row in connection.execute("SELECT id FROM clusters ORDER BY name")]

    def exists(self, cluster_id: int) -> bool:
        with self._connection_scope() as connection:
            return bool(connection.execute("SELECT 1 FROM clusters WHERE id=?", (cluster_id,)).fetchone())

    def record_in_connection(self, connection, cluster_id: int) -> dict | None:
        row = connection.execute("SELECT * FROM clusters WHERE id=?", (cluster_id,)).fetchone()
        return dict(row) if row else None

    def id_for_name_in_connection(self, connection, name: str) -> int | None:
        row = connection.execute("SELECT id FROM clusters WHERE name=?", (name,)).fetchone()
        return int(row["id"]) if row else None

    def next_theme_color_in_connection(self, connection, palette: tuple[str, ...]) -> str:
        used = {
            str(row["theme_color"]).upper()
            for row in connection.execute("SELECT theme_color FROM clusters WHERE theme_color IS NOT NULL")
        }
        return next(
            (color for color in palette if color not in used),
            palette[len(used) % len(palette)],
        )

    def create_in_connection(self, connection, values: dict) -> int:
        cursor = connection.execute(
            "INSERT INTO clusters(name,slug,ports_json,role_ports_json,secrets_json,observability_json,"
            "theme_color,desired_version,network_defaults_json,elasticsearch_settings_json,zoning_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                values["name"], values["slug"], values["ports_json"], values["role_ports_json"],
                values["secrets_json"], values["observability_json"], values["theme_color"],
                values["desired_version"], values["network_defaults_json"],
                values["elasticsearch_settings_json"], values["zoning_json"],
            ),
        )
        return int(cursor.lastrowid)

    def update_in_connection(self, connection, cluster_id: int, values: dict) -> bool:
        cursor = connection.execute(
            "UPDATE clusters SET name=?,slug=?,ports_json=?,role_ports_json=?,theme_color=?,desired_version=?,"
            "network_defaults_json=?,elasticsearch_settings_json=?,zoning_json=? WHERE id=?",
            (
                values["name"], values["slug"], values["ports_json"], values["role_ports_json"],
                values["theme_color"], values["desired_version"], values["network_defaults_json"],
                values["elasticsearch_settings_json"], values["zoning_json"], cluster_id,
            ),
        )
        return bool(cursor.rowcount)

    def update_provider_in_connection(self, connection, cluster_id: int, expected_revision: int, values: dict) -> bool:
        cursor = connection.execute(
            "UPDATE clusters SET provider_type=?,ownership_state=?,maintenance_backend=?,"
            "provider_capabilities_json=?,provider_connection_json=?,expected_cluster_uuid=?,"
            "provider_revision=provider_revision+1 WHERE id=? AND provider_revision=?",
            (
                values["provider_type"], values["ownership_state"], values["maintenance_backend"],
                values["provider_capabilities_json"], values["provider_connection_json"],
                values["expected_cluster_uuid"], cluster_id, expected_revision,
            ),
        )
        return bool(cursor.rowcount)

    def delete_in_connection(self, connection, cluster_id: int) -> bool:
        cursor = connection.execute("DELETE FROM clusters WHERE id=?", (cluster_id,))
        return bool(cursor.rowcount)

    def secrets_json(self, cluster_id: int) -> str:
        """Return the encrypted cluster credential envelope for its owner."""

        with self._connection_scope() as connection:
            row = self.secrets_json_row_in_connection(connection, cluster_id)
        if not row:
            raise KeyError(cluster_id)
        return str(row["secrets_json"])

    def secrets_json_row_in_connection(self, connection, cluster_id: int):
        """Return the sealed cluster credentials within an existing transaction."""

        return connection.execute(
            "SELECT secrets_json FROM clusters WHERE id=?", (cluster_id,)
        ).fetchone()

    def update_elasticsearch_settings_in_connection(
        self,
        connection,
        cluster_id: int,
        settings_json: str,
    ) -> None:
        """Persist validated desired Elasticsearch settings for one cluster."""

        cursor = connection.execute(
            "UPDATE clusters SET elasticsearch_settings_json=? WHERE id=?",
            (settings_json, cluster_id),
        )
        if not cursor.rowcount:
            raise KeyError(cluster_id)

    def slug(self, cluster_id: int) -> str:
        """Return the workload namespace label owned by a cluster."""

        with self._connection_scope() as connection:
            row = connection.execute(
                "SELECT slug FROM clusters WHERE id=?", (cluster_id,)
            ).fetchone()
        if not row:
            raise KeyError(cluster_id)
        return str(row["slug"])

    def replace_secrets_json(self, cluster_id: int, encrypted_secrets: str) -> None:
        """Persist a newly sealed credential envelope through the cluster owner."""

        with self._connection_scope() as connection:
            cursor = connection.execute(
                "UPDATE clusters SET secrets_json=? WHERE id=?",
                (encrypted_secrets, cluster_id),
            )
        if not cursor.rowcount:
            raise KeyError(cluster_id)

    def zoning_observation(self, cluster_id: int) -> dict | None:
        with self._connection_scope() as connection:
            row = connection.execute(
                "SELECT applied_mode,applied_zones_json FROM cluster_zoning_observations "
                "WHERE cluster_id=?",
                (cluster_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "applied_mode": str(row["applied_mode"] or "disabled"),
            "applied_zones": json.loads(row["applied_zones_json"] or "[]"),
        }

    def zoning_observation_record_in_connection(self, connection, cluster_id: int) -> dict | None:
        row = connection.execute(
            "SELECT * FROM cluster_zoning_observations WHERE cluster_id=?", (cluster_id,)
        ).fetchone()
        return dict(row) if row else None

    def memberships_in_connection(self, connection, cluster_id: int) -> list[dict]:
        rows = connection.execute(
            "SELECT cluster_id,node_id,network_mode,data_interface,data_address,user_interface,user_address "
            "FROM memberships WHERE cluster_id=? ORDER BY node_id",
            (cluster_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def memberships_for_node_in_connection(self, connection, node_id: int) -> list[dict]:
        rows = connection.execute(
            "SELECT cluster_id,node_id,network_mode,data_interface,data_address,user_interface,user_address "
            "FROM memberships WHERE node_id=? ORDER BY cluster_id",
            (node_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def membership_in_connection(self, connection, cluster_id: int, node_id: int) -> dict | None:
        row = connection.execute(
            "SELECT cluster_id,node_id,network_mode,data_interface,data_address,user_interface,user_address "
            "FROM memberships WHERE cluster_id=? AND node_id=?",
            (cluster_id, node_id),
        ).fetchone()
        return dict(row) if row else None

    def membership_exists_in_connection(self, connection, cluster_id: int, node_id: int) -> bool:
        """Return membership existence without exposing the table to callers."""

        return self.membership_in_connection(connection, cluster_id, node_id) is not None

    def record_runtime_zoning(
        self,
        cluster_id: int,
        *,
        applied_mode: str,
        applied_zones: list[str],
        observed_zones: dict[str, str],
        status: str,
        last_error: str,
    ) -> None:
        """Upsert a runtime zoning projection without exposing its table."""

        with self._connection_scope() as connection:
            connection.execute(
                "INSERT INTO cluster_zoning_observations(cluster_id,applied_mode,applied_zones_json,observed_zones_json,status,observed_at,last_error) "
                "VALUES (?,?,?,?,?,CURRENT_TIMESTAMP,?) "
                "ON CONFLICT(cluster_id) DO UPDATE SET "
                "observed_zones_json=excluded.observed_zones_json,status=excluded.status,"
                "observed_at=excluded.observed_at,last_error=excluded.last_error",
                (
                    cluster_id,
                    applied_mode,
                    json.dumps(applied_zones),
                    json.dumps(observed_zones),
                    status,
                    last_error,
                ),
            )

    def record_zoning_apply_in_connection(
        self,
        connection,
        cluster_id: int,
        *,
        applied_mode: str,
        applied_zones: list[str],
        observed_zones: dict[str, str],
        status: str,
        run_id: int | None,
        last_error: str = "",
    ) -> None:
        connection.execute(
            "INSERT INTO cluster_zoning_observations(cluster_id,applied_mode,applied_zones_json,observed_zones_json,status,last_run_id,observed_at,last_error) "
            "VALUES (?,?,?,?,?,?,CURRENT_TIMESTAMP,?) ON CONFLICT(cluster_id) DO UPDATE SET "
            "applied_mode=excluded.applied_mode,applied_zones_json=excluded.applied_zones_json,"
            "observed_zones_json=excluded.observed_zones_json,status=excluded.status,last_run_id=excluded.last_run_id,"
            "observed_at=CURRENT_TIMESTAMP,last_error=excluded.last_error",
            (cluster_id, applied_mode, json.dumps(applied_zones), json.dumps(observed_zones), status, run_id, last_error),
        )

    def record_zoning_failure_in_connection(self, connection, cluster_id: int, run_id: int, error: str) -> None:
        connection.execute(
            "INSERT INTO cluster_zoning_observations(cluster_id,status,last_run_id,observed_at,last_error) "
            "VALUES (?,'failed',?,CURRENT_TIMESTAMP,?) ON CONFLICT(cluster_id) DO UPDATE SET "
            "status='failed',last_run_id=excluded.last_run_id,observed_at=CURRENT_TIMESTAMP,"
            "last_error=excluded.last_error",
            (cluster_id, run_id, error[:500]),
        )

    def update_observed_zones_in_connection(
        self, connection, cluster_id: int, observed_zones: dict[str, str], run_id: int
    ) -> None:
        connection.execute(
            "UPDATE cluster_zoning_observations SET observed_zones_json=?,observed_at=CURRENT_TIMESTAMP,"
            "last_run_id=?,last_error='' WHERE cluster_id=?",
            (json.dumps(observed_zones), run_id, cluster_id),
        )

    def set_expected_cluster_uuid_if_missing(self, cluster_id: int, cluster_uuid: str) -> None:
        """Capture a discovered cluster identity once without replacing an import guard."""

        with self._connection_scope() as connection:
            connection.execute(
                "UPDATE clusters SET expected_cluster_uuid=? "
                "WHERE id=? AND expected_cluster_uuid IS NULL",
                (cluster_uuid, cluster_id),
            )

    def has_membership_for_node_in_connection(self, connection, node_id: int) -> bool:
        return bool(connection.execute("SELECT 1 FROM memberships WHERE node_id=?", (node_id,)).fetchone())

    def insert_membership_in_connection(self, connection, cluster_id: int, membership) -> None:
        """Persist a cluster host binding across both supported membership schemas."""

        columns = SchemaIntrospection(connection).columns("memberships")
        if "advertised_address" in columns:
            connection.execute(
                "INSERT INTO memberships(cluster_id,node_id,advertised_address,network_mode,data_interface,data_address,user_interface,user_address) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    cluster_id,
                    membership.node_id,
                    membership.user_address,
                    membership.network_mode,
                    membership.data_interface,
                    membership.data_address,
                    membership.user_interface,
                    membership.user_address,
                ),
            )
            return
        connection.execute(
            "INSERT INTO memberships(cluster_id,node_id,network_mode,data_interface,data_address,user_interface,user_address) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                cluster_id,
                membership.node_id,
                membership.network_mode,
                membership.data_interface,
                membership.data_address,
                membership.user_interface,
                membership.user_address,
            ),
        )

    def update_membership_in_connection(self, connection, cluster_id: int, node_id: int, membership) -> bool:
        cursor = connection.execute(
            "UPDATE memberships SET network_mode=?,data_interface=?,data_address=?,user_interface=?,user_address=? "
            "WHERE cluster_id=? AND node_id=?",
            (
                membership.network_mode,
                membership.data_interface,
                membership.data_address,
                membership.user_interface,
                membership.user_address,
                cluster_id,
                node_id,
            ),
        )
        return bool(cursor.rowcount)

    def delete_membership_in_connection(self, connection, cluster_id: int, node_id: int) -> None:
        connection.execute("DELETE FROM memberships WHERE cluster_id=? AND node_id=?", (cluster_id, node_id))

    def update_zoning_in_connection(self, connection, cluster_id: int, zoning_json: str) -> bool:
        cursor = connection.execute(
            "UPDATE clusters SET zoning_json=? WHERE id=?", (zoning_json, cluster_id)
        )
        return bool(cursor.rowcount)

    def update_observability_in_connection(self, connection, cluster_id: int, observability_json: str) -> bool:
        """Persist the desired cluster log-monitoring configuration."""

        cursor = connection.execute(
            "UPDATE clusters SET observability_json=? WHERE id=?",
            (observability_json, cluster_id),
        )
        return bool(cursor.rowcount)
