"""Workload-domain application service."""

from __future__ import annotations

from typing import Callable

from fastapi import HTTPException

from .repository import WorkloadRepository


class WorkloadService:
    def __init__(self, db_factory: Callable):
        self.repository = WorkloadRepository(db_factory)

    def active_count(self, cluster_id: int) -> int:
        return self.repository.active_count(cluster_id)

    def active_ids(self, cluster_id: int) -> list[int]:
        return self.repository.active_ids(cluster_id)

    def require_initial_master_batch(self, connection, assignment: dict) -> None:
        """Keep the initial secure bootstrap on the staged batch path."""

        if assignment["role"] != "master":
            return
        active = WorkloadRepository.from_connection(connection).active_for_cluster_in_connection(
            connection, int(assignment["cluster_id"])
        )
        initial_master = next((item for item in active if item["role"] == "master"), None)
        if initial_master and int(initial_master["id"]) == int(assignment["id"]):
            raise HTTPException(
                422,
                "The first master must be applied in a staged change set with a Hot data-content workload",
            )
