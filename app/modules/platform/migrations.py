"""Additive migration registration owned by the platform database layer."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from typing import Any


class MigrationRegistry:
    """Own the durable ledger used by additive module migrations."""

    def __init__(self, connection: Any, table_name: str):
        if not table_name or not table_name.replace("_", "").isalnum():
            raise ValueError("Invalid migration table name")
        self.connection = connection
        self.table_name = table_name

    def ensure(self) -> None:
        self.connection.execute(f"""
            CREATE TABLE IF NOT EXISTS {self.table_name} (
              version INTEGER PRIMARY KEY,
              name TEXT NOT NULL,
              checksum TEXT NOT NULL,
              applied_at TEXT NOT NULL
            )
        """)

    def applied(self) -> dict[int, Any]:
        rows = self.connection.execute(
            f"SELECT version,name,checksum FROM {self.table_name} ORDER BY version"
        ).fetchall()
        return {(row["version"] if hasattr(row, "keys") else row[0]): row for row in rows}

    def record(self, version: int, name: str, checksum: str, applied_at: str) -> None:
        self.connection.execute(
            f"INSERT INTO {self.table_name}(version,name,checksum,applied_at) VALUES(?,?,?,?)",
            (version, name, checksum, applied_at),
        )


class MigrationDriftError(ValueError):
    """Raised when a durable migration ledger disagrees with known code."""


Migration = Callable[[Any], None]


def run_migrations(connection: Any, migrations: Iterable[Migration]) -> None:
    """Run registered idempotent migrations in declaration order."""

    for migration in migrations:
        migration(connection)


def run_registered_migrations(
    connection: Any,
    migrations: Iterable[tuple[int, str, str, Migration]],
    *,
    table_name: str = "maintenance_schema_migrations",
    timestamp: Callable[[], str] | None = None,
) -> None:
    """Atomically apply known additive migrations and record their ledger rows.

    A failed callback leaves neither its schema/data changes nor a durable
    ledger record behind.  On the next startup the complete migration is
    retried from the preceding recorded version.
    """

    definitions = tuple(migrations)
    versions = [version for version, _name, _checksum, _callback in definitions]
    if any(not isinstance(version, int) or version < 1 for version in versions):
        raise ValueError("Migration versions must be positive integers")
    if versions != sorted(versions) or len(versions) != len(set(versions)):
        raise ValueError("Migration versions must be unique and ordered")
    if any(not name or not checksum for _version, name, checksum, _callback in definitions):
        raise ValueError("Migration names and checksums are required")

    connection.execute("SAVEPOINT platform_registered_migrations")
    try:
        registry = MigrationRegistry(connection, table_name)
        registry.ensure()
        applied = registry.applied()
        known = {version: (name, checksum) for version, name, checksum, _callback in definitions}
        unknown = sorted(set(applied) - set(known))
        if unknown:
            raise MigrationDriftError(
                "Unknown migration versions: " + ", ".join(str(version) for version in unknown)
            )
        for version, row in applied.items():
            name, checksum = known[version]
            recorded_name = row["name"] if hasattr(row, "keys") else row[1]
            recorded_checksum = row["checksum"] if hasattr(row, "keys") else row[2]
            if (recorded_name, recorded_checksum) != (name, checksum):
                raise MigrationDriftError(f"Migration {version} checksum does not match")
        now = timestamp or (lambda: datetime.now(timezone.utc).isoformat())
        for version, name, checksum, callback in definitions:
            if version in applied:
                continue
            callback(connection)
            registry.record(version, name, checksum, now())
        connection.execute("RELEASE SAVEPOINT platform_registered_migrations")
    except Exception:
        connection.execute("ROLLBACK TO SAVEPOINT platform_registered_migrations")
        connection.execute("RELEASE SAVEPOINT platform_registered_migrations")
        raise
