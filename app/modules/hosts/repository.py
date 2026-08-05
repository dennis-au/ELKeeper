"""Persistence boundary for the nodes table."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Callable


class HostRepository:
    def __init__(self, db_factory: Callable | None = None, *, connection=None):
        if db_factory is None and connection is None:
            raise ValueError("HostRepository requires a database factory or connection")
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

    def list(self) -> list[dict]:
        with self._connection_scope() as connection:
            rows = connection.execute("SELECT * FROM nodes ORDER BY name").fetchall()
        return [self._public(row) for row in rows]

    def list_enabled(self) -> list[dict]:
        """Return enabled hosts in stable inventory order for runtime consumers."""

        with self._connection_scope() as connection:
            rows = connection.execute(
                "SELECT * FROM nodes WHERE enabled=1 ORDER BY id"
            ).fetchall()
        return [self._public(row) for row in rows]

    def get(self, node_id: int) -> dict | None:
        with self._connection_scope() as connection:
            row = connection.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
        return self._public(row) if row else None

    def get_enabled(self, node_id: int) -> dict | None:
        """Return one enabled host without exposing a SQL projection to callers."""

        with self._connection_scope() as connection:
            row = connection.execute(
                "SELECT * FROM nodes WHERE id=? AND enabled=1", (node_id,)
            ).fetchone()
        return self._public(row) if row else None

    def is_enabled_in_connection(self, connection, node_id: int) -> bool:
        """Check host availability through the hosts persistence boundary."""

        row = connection.execute("SELECT enabled FROM nodes WHERE id=?", (node_id,)).fetchone()
        return bool(row and row["enabled"])

    def enabled_names_in_connection(self, connection, node_ids: list[int]) -> list[str]:
        """Return enabled host names in caller-supplied inventory order."""

        if not node_ids:
            return []
        placeholders = ",".join("?" * len(node_ids))
        rows = connection.execute(
            "SELECT id,name FROM nodes WHERE enabled=1 AND id IN (" + placeholders + ")",
            node_ids,
        ).fetchall()
        names = {int(row["id"]): str(row["name"]) for row in rows}
        return [names[node_id] for node_id in node_ids if node_id in names]

    def create(self, host: dict) -> int:
        with self._connection_scope() as connection:
            cursor = connection.execute(
                "INSERT INTO nodes(name,address,ssh_port,ssh_user,enabled,zone_id) VALUES (?,?,?,?,?,?)",
                (host["name"], host["address"], host["ssh_port"], host["ssh_user"], host["enabled"], host.get("zone_id")),
            )
            return int(cursor.lastrowid)

    def enroll_pending_in_connection(self, connection, enrollment: dict, *, name: str, host_key: str) -> dict:
        """Create the disabled inventory record used until enrollment verifies it."""

        cursor = connection.execute(
            "INSERT INTO nodes(name,address,ssh_port,ssh_user,enabled,ssh_host_key,ssh_auth_state,zone_id) "
            "VALUES (?,?,?,?,?,?, 'pending',?)",
            (
                name,
                enrollment["address"],
                enrollment["ssh_port"],
                enrollment["ssh_user"],
                0,
                host_key,
                enrollment.get("zone_id"),
            ),
        )
        row = connection.execute("SELECT * FROM nodes WHERE id=?", (cursor.lastrowid,)).fetchone()
        return self._public(row)

    def update_in_connection(self, connection, node_id: int, host: dict, *, host_key: str) -> bool:
        cursor = connection.execute(
            "UPDATE nodes SET name=?,address=?,ssh_port=?,ssh_user=?,enabled=?,ssh_host_key=? WHERE id=?",
            (host["name"], host["address"], host["ssh_port"], host["ssh_user"], host["enabled"], host_key, node_id),
        )
        return bool(cursor.rowcount)

    def set_zone_in_connection(self, connection, node_id: int, zone_id: str) -> None:
        connection.execute("UPDATE nodes SET zone_id=? WHERE id=?", (zone_id, node_id))

    def disable_legacy_known_hosts_in_connection(self, connection, node_id: int) -> None:
        connection.execute("UPDATE nodes SET legacy_known_hosts_disabled=1 WHERE id=?", (node_id,))

    def host_key_records_in_connection(self, connection) -> list[dict]:
        """Return the non-secret inventory needed to display pinned host keys."""

        rows = connection.execute(
            "SELECT id,name,address,ssh_port,ssh_host_key FROM nodes "
            "WHERE ssh_host_key<>'' ORDER BY name,id"
        ).fetchall()
        return [dict(row) for row in rows]

    def clear_host_key_in_connection(self, connection, node_id: int) -> bool:
        """Remove one controller-recorded SSH host-key pin."""

        cursor = connection.execute(
            "UPDATE nodes SET ssh_host_key='' WHERE id=? AND ssh_host_key<>''",
            (node_id,),
        )
        return bool(cursor.rowcount)

    def delete_in_connection(self, connection, node_id: int) -> bool:
        cursor = connection.execute("DELETE FROM nodes WHERE id=?", (node_id,))
        return bool(cursor.rowcount)

    def enabled_count_in_connection(self, connection) -> int:
        """Return the number of enabled hosts for identity lifecycle decisions."""

        return int(
            connection.execute("SELECT COUNT(*) AS count FROM nodes WHERE enabled=1").fetchone()["count"]
        )

    def candidate_key_installation_names_in_connection(self, connection, key_ids: list[str]) -> list[str]:
        """Return hosts that still reference one of the supplied staged keys."""

        if not key_ids:
            return []
        placeholders = ",".join("?" * len(key_ids))
        rows = connection.execute(
            "SELECT name FROM nodes WHERE candidate_key_id IN (" + placeholders + ") ORDER BY name",
            key_ids,
        ).fetchall()
        return [str(row["name"]) for row in rows]

    def missing_candidate_key_names_in_connection(self, connection, key_id: str) -> list[str]:
        """Return enabled hosts that have not verified the staged controller key."""

        rows = connection.execute(
            "SELECT name FROM nodes WHERE enabled=1 AND candidate_key_id<>? ORDER BY name",
            (key_id,),
        ).fetchall()
        return [str(row["name"]) for row in rows]

    def activate_candidate_key_in_connection(self, connection, key_id: str) -> None:
        """Make a verified candidate the active identity on every enrolled host."""

        connection.execute(
            "UPDATE nodes SET ssh_key_id=?,candidate_key_id='',ssh_auth_state='controller_key' "
            "WHERE candidate_key_id=?",
            (key_id, key_id),
        )

    def pinned_host_keys_in_connection(self, connection, node_ids: list[int] | None = None) -> list[dict]:
        """Expose the safe public host-key projection used to build known-hosts files."""

        if node_ids:
            placeholders = ",".join("?" * len(node_ids))
            rows = connection.execute(
                "SELECT address,ssh_port,ssh_host_key FROM nodes "
                "WHERE ssh_host_key<>'' AND id IN (" + placeholders + ") ORDER BY id",
                node_ids,
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT address,ssh_port,ssh_host_key FROM nodes WHERE ssh_host_key<>'' ORDER BY id"
            ).fetchall()
        return [dict(row) for row in rows]

    def records_for_ids_in_connection(self, connection, node_ids: list[int]) -> dict[int, dict]:
        """Return public inventory records keyed by id for cross-domain projections."""

        if not node_ids:
            return {}
        placeholders = ",".join("?" * len(node_ids))
        rows = connection.execute(
            "SELECT * FROM nodes WHERE id IN (" + placeholders + ")",
            node_ids,
        ).fetchall()
        return {int(row["id"]): self._public(row) for row in rows}

    def inventory_records_in_connection(self, connection, node_ids: list[int] | None = None) -> list[dict]:
        """Return the host-owned records required to build a temporary inventory."""

        if node_ids:
            placeholders = ",".join("?" * len(node_ids))
            rows = connection.execute(
                "SELECT * FROM nodes WHERE id IN (" + placeholders + ") ORDER BY name",
                node_ids,
            ).fetchall()
        else:
            rows = connection.execute("SELECT * FROM nodes WHERE enabled=1 ORDER BY name").fetchall()
        return [self._public(row) for row in rows]

    def mark_candidate_key_installed_in_connection(self, connection, node_id: int, key_id: str, enabled: bool) -> None:
        connection.execute(
            "UPDATE nodes SET candidate_key_id=?,ssh_auth_state='candidate_ready',enabled=? WHERE id=?",
            (key_id, int(enabled), node_id),
        )

    def mark_controller_key_installed_in_connection(self, connection, node_id: int, key_id: str, enabled: bool) -> None:
        connection.execute(
            "UPDATE nodes SET ssh_key_id=?,candidate_key_id='',ssh_auth_state='controller_key',enabled=? WHERE id=?",
            (key_id, int(enabled), node_id),
        )

    def mark_legacy_enrollment_in_connection(self, connection, node_id: int, enabled: bool) -> None:
        connection.execute(
            "UPDATE nodes SET ssh_auth_state='legacy',enabled=? WHERE id=?",
            (int(enabled), node_id),
        )

    def mark_enrollment_pending_in_connection(self, connection, node_id: int) -> None:
        connection.execute("UPDATE nodes SET ssh_auth_state='pending',enabled=0 WHERE id=?", (node_id,))

    def rename_in_connection(self, connection, node_id: int, name: str) -> None:
        connection.execute("UPDATE nodes SET name=? WHERE id=?", (name, node_id))

    def name_exists_in_connection(self, connection, name: str, excluded_node_id: int) -> bool:
        return bool(
            connection.execute(
                "SELECT 1 FROM nodes WHERE name=? AND id<>?", (name, excluded_node_id)
            ).fetchone()
        )

    def restore_zone_in_connection(self, connection, node_id: int, zone_id: str | None) -> None:
        connection.execute("UPDATE nodes SET zone_id=? WHERE id=?", (zone_id, node_id))

    @staticmethod
    def _public(row) -> dict:
        value = dict(row)
        value["enabled"] = bool(value.get("enabled"))
        value["legacy_known_hosts_disabled"] = bool(value.get("legacy_known_hosts_disabled"))
        return value
