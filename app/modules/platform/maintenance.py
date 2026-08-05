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


# A runtime environment request is necessary but intentionally not sufficient
# for a protected mutation.  The release artifact must explicitly add a
# capability to this allow-list only after its phase gate, packaging proof, and
# required live acceptance have been recorded.  Keeping this empty is the
# current safe release state.
APPROVED_MUTATION_CAPABILITIES: frozenset[str] = frozenset()


def _approved_environment_capability(capability: str, variable: str) -> bool:
    return _environment_flag(variable) and capability in APPROVED_MUTATION_CAPABILITIES


MAINTENANCE_CAPABILITIES = {
    "planning": _environment_flag("MAINTENANCE_PLANNING_ENABLED"),
    "host_reboot": _approved_environment_capability("host_reboot", "MAINTENANCE_HOST_REBOOT_ENABLED"),
    "rolling_restart": _approved_environment_capability("rolling_restart", "MAINTENANCE_ROLLING_RESTART_ENABLED"),
    "upgrade": _approved_environment_capability("upgrade", "MAINTENANCE_UPGRADE_ENABLED"),
    "evacuation": _approved_environment_capability("evacuation", "MAINTENANCE_EVACUATION_ENABLED"),
    "node_shutdown_backend": _approved_environment_capability(
        "node_shutdown_backend", "MAINTENANCE_NODE_SHUTDOWN_BACKEND_ENABLED"
    ),
}


def capability_snapshot() -> dict[str, bool]:
    """Return a copy suitable for hashing or serializing in API responses."""

    return dict(MAINTENANCE_CAPABILITIES)
