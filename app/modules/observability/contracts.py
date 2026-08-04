"""Bounded telemetry primitives."""

from __future__ import annotations


def bounded_history(values: list, limit: int = 120) -> list:
    """Keep only the newest telemetry samples in memory."""

    if limit < 1:
        raise ValueError("Telemetry history limit must be positive")
    return list(values[-limit:])
