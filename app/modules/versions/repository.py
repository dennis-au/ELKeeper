"""Persistence boundary for runtime workload version observations."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Callable


class VersionRepository:
    def __init__(self, db_factory: Callable | None = None, *, connection=None):
        if db_factory is None and connection is None:
            raise ValueError("VersionRepository requires a database factory or connection")
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

    def record_runtime(
        self,
        assignment_id: int,
        *,
        image: str,
        digest: str,
        version: str,
        running: bool,
        cached: bool,
        error: str,
        filebeat_state: str | None = None,
        filebeat_error: str = "",
    ) -> None:
        """Upsert one observed workload image without changing desired state."""

        with self._connection_scope() as connection:
            if filebeat_state is not None:
                connection.execute(
                    "INSERT INTO workload_observations(assignment_id,image,digest,version,running,cached,observed_at,error,filebeat_state,filebeat_observed_at,filebeat_error) "
                    "VALUES (?,?,?,?,?,?,CURRENT_TIMESTAMP,?, ?,CURRENT_TIMESTAMP,?) "
                    "ON CONFLICT(assignment_id) DO UPDATE SET "
                    "image=excluded.image,digest=excluded.digest,version=excluded.version,"
                    "running=excluded.running,cached=excluded.cached,observed_at=excluded.observed_at,error=excluded.error,"
                    "filebeat_state=excluded.filebeat_state,filebeat_observed_at=excluded.filebeat_observed_at,"
                    "filebeat_error=excluded.filebeat_error",
                    (
                        assignment_id,
                        image,
                        digest,
                        version,
                        int(running),
                        int(cached),
                        error,
                        filebeat_state,
                        filebeat_error,
                    ),
                )
                return
            connection.execute(
                "INSERT INTO workload_observations(assignment_id,image,digest,version,running,cached,observed_at,error) "
                "VALUES (?,?,?,?,?,?,CURRENT_TIMESTAMP,?) "
                "ON CONFLICT(assignment_id) DO UPDATE SET "
                "image=excluded.image,digest=excluded.digest,version=excluded.version,"
                "running=excluded.running,cached=excluded.cached,observed_at=excluded.observed_at,error=excluded.error",
                (assignment_id, image, digest, version, int(running), int(cached), error),
            )

    def record_filebeat_runtime(self, assignment_id: int, *, state: str, error: str) -> None:
        """Record companion state without rewriting the primary image observation."""

        with self._connection_scope() as connection:
            connection.execute(
                "INSERT INTO workload_observations(assignment_id,filebeat_state,filebeat_observed_at,filebeat_error) "
                "VALUES (?,?,CURRENT_TIMESTAMP,?) "
                "ON CONFLICT(assignment_id) DO UPDATE SET "
                "filebeat_state=excluded.filebeat_state,"
                "filebeat_observed_at=excluded.filebeat_observed_at,"
                "filebeat_error=excluded.filebeat_error",
                (assignment_id, state, error),
            )

    def observations_for_assignments_in_connection(self, connection, assignment_ids: list[int]) -> dict[int, dict]:
        """Return version-owned runtime observations for a workload projection."""

        if not assignment_ids:
            return {}
        placeholders = ",".join("?" * len(assignment_ids))
        rows = connection.execute(
            "SELECT * FROM workload_observations WHERE assignment_id IN (" + placeholders + ")",
            assignment_ids,
        ).fetchall()
        return {int(row["assignment_id"]): dict(row) for row in rows}
