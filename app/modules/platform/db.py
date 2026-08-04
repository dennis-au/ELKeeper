"""SQLite connection contract owned by the platform database module."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sqlite3
from typing import Iterator

from .config import RuntimePaths


@contextmanager
def connect(path: Path | str) -> Iterator[sqlite3.Connection]:
    """Open a foreign-key-enabled SQLite connection and commit atomically."""

    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


@contextmanager
def control_db(path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    """Open the controller database at the configured persistence boundary."""

    database_path = path or (RuntimePaths.from_environment().data / "control.db")
    with connect(database_path) as connection:
        yield connection


def table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    """Check schema presence through the platform database contract."""

    if not table_name or not table_name.replace("_", "").isalnum():
        raise ValueError("Invalid table name")
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None
