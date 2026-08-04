"""Workload-domain application service."""

from __future__ import annotations

from typing import Callable

from .repository import WorkloadRepository


class WorkloadService:
    def __init__(self, db_factory: Callable):
        self.repository = WorkloadRepository(db_factory)

    def active_count(self, cluster_id: int) -> int:
        return self.repository.active_count(cluster_id)

    def active_ids(self, cluster_id: int) -> list[int]:
        return self.repository.active_ids(cluster_id)
