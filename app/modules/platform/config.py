"""Runtime configuration and path registry.

The settings object is deliberately dependency-free so domain modules can use
it without importing FastAPI or the application assembly module.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Callable


PERSISTENT_DATA_DIR = Path("/var/lib/elastic-control")


def app_data_dir(persistent_data_dir: Path = PERSISTENT_DATA_DIR) -> Path:
    """Resolve the persistent data mount, retaining the legacy fallback rule."""

    configured = Path(os.getenv("APP_DATA_DIR", str(persistent_data_dir)))
    if configured.is_absolute() or not persistent_data_dir.exists():
        return configured
    return persistent_data_dir


def environment_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class RuntimePaths:
    """Named filesystem boundaries shared by platform services."""

    data: Path
    runs: Path
    inventories: Path
    variables: Path
    runtime: Path
    ssh_runtime: Path

    @classmethod
    def from_environment(cls) -> "RuntimePaths":
        data = app_data_dir()
        runtime = Path(os.getenv("APP_RUNTIME_DIR", "/run/elastic-control"))
        return cls(
            data=data,
            runs=data / "runs",
            inventories=data / "inventory",
            variables=data / "variables",
            runtime=runtime,
            ssh_runtime=runtime / "ssh",
        )


def get_setting(db_factory: Callable, key: str, default: str | None = None) -> str | None:
    """Read a controller setting through the platform configuration boundary."""

    with db_factory() as connection:
        row = connection.execute("SELECT value FROM controller_settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(db_factory: Callable, key: str, value: str) -> None:
    """Persist a controller setting through the platform configuration boundary."""

    with db_factory() as connection:
        connection.execute(
            "INSERT INTO controller_settings(key,value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
