"""Public maintenance capability contract.

Feature routers consume this shared mapping, while the application assembly
owns environment setup and can continue exposing the legacy constant for
compatibility with existing callers and tests.
"""

from __future__ import annotations

import os


def _environment_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


MAINTENANCE_CAPABILITIES = {
    "planning": _environment_flag("MAINTENANCE_PLANNING_ENABLED"),
    "host_reboot": _environment_flag("MAINTENANCE_HOST_REBOOT_ENABLED"),
    "rolling_restart": _environment_flag("MAINTENANCE_ROLLING_RESTART_ENABLED"),
    "upgrade": _environment_flag("MAINTENANCE_UPGRADE_ENABLED"),
    "evacuation": _environment_flag("MAINTENANCE_EVACUATION_ENABLED"),
    "node_shutdown_backend": _environment_flag("MAINTENANCE_NODE_SHUTDOWN_BACKEND_ENABLED"),
}


def capability_snapshot() -> dict[str, bool]:
    """Return a copy suitable for hashing or serializing in API responses."""

    return dict(MAINTENANCE_CAPABILITIES)
