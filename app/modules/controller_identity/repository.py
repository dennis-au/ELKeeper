"""Persistence boundary for controller-owned SSH identity metadata."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Callable


class ControllerIdentityRepository:
    """Own controller SSH-key rows without exposing direct SQL to callers."""

    def __init__(self, db_factory: Callable | None = None, *, connection=None):
        if db_factory is None and connection is None:
            raise ValueError("ControllerIdentityRepository requires a database factory or connection")
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

    def active_and_candidate(self):
        with self._connection_scope() as connection:
            return self.active_and_candidate_in_connection(connection)

    def active_and_candidate_in_connection(self, connection):
        rows = connection.execute(
            "SELECT * FROM controller_ssh_keys WHERE state IN ('active','candidate') ORDER BY id DESC"
        ).fetchall()
        active = next((row for row in rows if row["state"] == "active"), None)
        candidate = next((row for row in rows if row["state"] == "candidate"), None)
        return active, candidate

    def candidate_key_ids_in_connection(self, connection) -> list[str]:
        rows = connection.execute(
            "SELECT key_id FROM controller_ssh_keys WHERE state='candidate'"
        ).fetchall()
        return [str(row["key_id"]) for row in rows]

    def state_for_key_in_connection(self, connection, key_id: str) -> str | None:
        row = connection.execute(
            "SELECT state FROM controller_ssh_keys WHERE key_id=?", (key_id,)
        ).fetchone()
        return str(row["state"]) if row else None

    def retire_candidates_in_connection(self, connection) -> None:
        connection.execute("UPDATE controller_ssh_keys SET state='retired' WHERE state='candidate'")

    def retire_active_in_connection(self, connection) -> None:
        connection.execute("UPDATE controller_ssh_keys SET state='retired' WHERE state='active'")

    def retire_by_id_in_connection(self, connection, record_id: int) -> None:
        connection.execute("UPDATE controller_ssh_keys SET state='retired' WHERE id=?", (record_id,))

    def activate_by_id_in_connection(self, connection, record_id: int) -> None:
        connection.execute("UPDATE controller_ssh_keys SET state='active' WHERE id=?", (record_id,))

    def create_in_connection(self, connection, *, key_id: str, algorithm: str, public_key: str,
                             private_key_encrypted: str, source: str, state: str):
        cursor = connection.execute(
            "INSERT INTO controller_ssh_keys(key_id,algorithm,public_key,private_key_encrypted,source,state) "
            "VALUES (?,?,?,?,?,?)",
            (key_id, algorithm, public_key, private_key_encrypted, source, state),
        )
        return connection.execute(
            "SELECT * FROM controller_ssh_keys WHERE id=?", (cursor.lastrowid,)
        ).fetchone()
