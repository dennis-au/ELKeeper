"""Bounded telemetry history and stream-token contracts."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from time import time
from contextlib import contextmanager
from typing import Callable, Generic, Iterable, TypeVar

T = TypeVar("T")


class BoundedHistory(Generic[T]):
    def __init__(self, limit: int = 120):
        if limit < 1:
            raise ValueError("history limit must be positive")
        self._values: deque[T] = deque(maxlen=limit)

    def append(self, value: T) -> None:
        self._values.append(value)

    def snapshot(self) -> tuple[T, ...]:
        return tuple(self._values)


@dataclass(frozen=True)
class StreamToken:
    value: str
    expires_at: float

    def valid(self, now: float | None = None) -> bool:
        return (now or time()) < self.expires_at


def runtime_observation(db_factory, node_id: int) -> dict | None:
    """Compatibility projection for the observability repository."""

    return ObservabilityRepository(db_factory).runtime_observation(node_id)


class ObservabilityRepository:
    """Persistence boundary for bounded host runtime observations."""

    def __init__(self, db_factory: Callable | None = None, *, connection=None):
        if db_factory is None and connection is None:
            raise ValueError("ObservabilityRepository requires a database factory or connection")
        if db_factory is not None and connection is not None:
            raise ValueError("Provide either a database factory or connection, not both")
        self._db = db_factory
        self._connection = connection

    @contextmanager
    def _connection_scope(self):
        if self._connection is not None:
            yield self._connection
            return
        assert self._db is not None
        with self._db() as connection:
            yield connection

    def runtime_observation(self, node_id: int) -> dict | None:
        with self._connection_scope() as connection:
            row = connection.execute(
                "SELECT * FROM host_runtime_observations WHERE node_id=?",
                (node_id,),
            ).fetchone()
        return dict(row) if row else None

    def runtime_observations(self) -> dict[int, dict]:
        """Return the latest durable state indexed by host identity."""

        with self._connection_scope() as connection:
            rows = connection.execute("SELECT * FROM host_runtime_observations").fetchall()
        return {int(row["node_id"]): dict(row) for row in rows}

    def record_host_runtime(self, node_id: int, state: dict, *, observed_at: str) -> None:
        """Upsert a redacted durable host projection from transient telemetry."""

        import json

        with self._connection_scope() as connection:
            connection.execute(
                "INSERT INTO host_runtime_observations(node_id,initialized,reachable,podman_socket_active,os_name,podman_version,observed_at,last_error,network_interfaces_json) "
                "VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(node_id) DO UPDATE SET "
                "initialized=excluded.initialized,reachable=excluded.reachable,"
                "podman_socket_active=excluded.podman_socket_active,os_name=excluded.os_name,"
                "podman_version=excluded.podman_version,observed_at=excluded.observed_at,"
                "last_error=excluded.last_error,network_interfaces_json=excluded.network_interfaces_json",
                (
                    node_id,
                    int(bool(state.get("initialized"))),
                    int(bool(state.get("reachable"))),
                    int(bool(state.get("podman_socket_active"))),
                    str(state.get("os_name") or ""),
                    str(state.get("podman_version") or ""),
                    observed_at,
                    str(state.get("last_error") or "")[:300],
                    json.dumps(state.get("network_interfaces") or {}, sort_keys=True),
                ),
            )
