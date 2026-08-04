"""Host-domain application service."""

from __future__ import annotations

from typing import Callable

from .repository import HostRepository


class HostService:
    """Keep route handlers independent from the nodes repository details."""

    def __init__(self, db_factory: Callable):
        self.repository = HostRepository(db_factory)

    def list(self) -> list[dict]:
        return self.repository.list()

    def get(self, node_id: int) -> dict | None:
        return self.repository.get(node_id)

    def create(self, host: dict) -> int:
        return self.repository.create(host)


def enabled_host(db_factory: Callable, node_id: int) -> dict | None:
    """Return the enabled inventory projection used by compatibility callers."""

    return HostRepository(db_factory).get_enabled(node_id)
