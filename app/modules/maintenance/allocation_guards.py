"""Durable ownership for Elasticsearch allocation guards."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from app.modules.maintenance.elasticsearch import (
    AllocationCleanupTrigger,
    AllocationGuardCheckpoint,
    AllocationGuardController,
    AllocationGuardPhase,
)
from app.modules.maintenance.store import (
    AllocationGuardConflict,
    AllocationGuardOwnershipError,
    AllocationGuardRecord,
    MaintenanceRepository,
    RecordNotFound,
)


class AllocationGuardService:
    """Persists cluster-wide guard ownership around remote guard operations."""

    def __init__(
        self,
        repository: MaintenanceRepository,
        controller: AllocationGuardController,
    ):
        self.repository = repository
        self.controller = controller

    async def capture(self, *, plan_id: str, cluster_id: int) -> AllocationGuardCheckpoint:
        current = self.repository.find_allocation_guard(
            cluster_id=cluster_id,
            owner_plan_id=plan_id,
        )
        if current is not None:
            if current.phase in {
                AllocationGuardPhase.CAPTURED.value,
                AllocationGuardPhase.ACTIVE.value,
                AllocationGuardPhase.RECOVERY_REQUIRED.value,
            }:
                return self._checkpoint(current)
            raise AllocationGuardConflict("Allocation guard has already been restored for this plan")
        active = self.repository.find_active_allocation_guard(cluster_id)
        if active is not None:
            raise AllocationGuardConflict("Cluster allocation guard is owned by another maintenance plan")

        checkpoint = await self.controller.capture(plan_id=plan_id, cluster_id=cluster_id)
        if checkpoint.phase != AllocationGuardPhase.CAPTURED:
            raise ValueError("Allocation guard capture did not return a captured checkpoint")
        self.repository.create_allocation_guard(
            cluster_id=cluster_id,
            owner_plan_id=plan_id,
            checkpoint=checkpoint.model_dump(mode="json"),
        )
        return checkpoint

    async def activate(self, *, plan_id: str, cluster_id: int) -> AllocationGuardCheckpoint:
        record = self._owned_active_record(plan_id=plan_id, cluster_id=cluster_id)
        checkpoint = self._checkpoint(record)
        if checkpoint.phase == AllocationGuardPhase.ACTIVE:
            return checkpoint
        if checkpoint.phase != AllocationGuardPhase.CAPTURED:
            raise AllocationGuardOwnershipError("Allocation guard cannot be activated in its current phase")
        try:
            result = await self.controller.activate(checkpoint)
            next_checkpoint = result.checkpoint
        except Exception:
            next_checkpoint = checkpoint.model_copy(
                update={"phase": AllocationGuardPhase.RECOVERY_REQUIRED}
            )
            self._persist(record, next_checkpoint)
            raise
        return self._persist(record, next_checkpoint)

    async def restore(
        self,
        *,
        plan_id: str,
        cluster_id: int,
        trigger: AllocationCleanupTrigger = AllocationCleanupTrigger.SUCCESS,
    ) -> AllocationGuardCheckpoint:
        record = self._owned_record(plan_id=plan_id, cluster_id=cluster_id)
        checkpoint = self._checkpoint(record)
        if checkpoint.phase == AllocationGuardPhase.RESTORED:
            return checkpoint
        result = await self.controller.restore(checkpoint, trigger=trigger)
        return self._persist(record, result.checkpoint)

    def _owned_active_record(self, *, plan_id: str, cluster_id: int) -> AllocationGuardRecord:
        record = self._owned_record(plan_id=plan_id, cluster_id=cluster_id)
        if record.phase == AllocationGuardPhase.RESTORED.value:
            raise AllocationGuardOwnershipError("Allocation guard has already been restored")
        return record

    def _owned_record(self, *, plan_id: str, cluster_id: int) -> AllocationGuardRecord:
        record = self.repository.find_allocation_guard(
            cluster_id=cluster_id,
            owner_plan_id=plan_id,
        )
        if record is not None:
            return record
        active = self.repository.find_active_allocation_guard(cluster_id)
        if active is not None:
            raise AllocationGuardOwnershipError("Cluster allocation guard is owned by another maintenance plan")
        raise RecordNotFound("Maintenance allocation guard was not found")

    @staticmethod
    def _checkpoint(record: AllocationGuardRecord) -> AllocationGuardCheckpoint:
        checkpoint = AllocationGuardCheckpoint.model_validate(record.checkpoint)
        if checkpoint.plan_id != record.owner_plan_id or checkpoint.cluster_id != record.cluster_id:
            raise ValueError("Persisted allocation guard checkpoint identity is invalid")
        if checkpoint.phase.value != record.phase:
            raise ValueError("Persisted allocation guard phase is inconsistent")
        return checkpoint

    def _persist(
        self,
        record: AllocationGuardRecord,
        checkpoint: AllocationGuardCheckpoint,
    ) -> AllocationGuardCheckpoint:
        persisted = self.repository.transition_allocation_guard(
            record.id,
            owner_plan_id=record.owner_plan_id,
            expected_revision=record.state_revision,
            phase=checkpoint.phase.value,
            checkpoint=checkpoint.model_dump(mode="json"),
        )
        return self._checkpoint(persisted)


class ClusterAllocationGuardRouter:
    """Lazily route one action's guards to their owning cluster client."""

    def __init__(
        self,
        service_factory: Callable[[int], Any],
        *,
        close_guard: Callable[[Any], Any] | None = None,
    ) -> None:
        self._service_factory = service_factory
        self._close_guard = close_guard
        self._services: dict[int, Any] = {}

    async def capture(self, *, plan_id: str, cluster_id: int):
        return await self._service(cluster_id).capture(plan_id=plan_id, cluster_id=cluster_id)

    async def activate(self, *, plan_id: str, cluster_id: int):
        return await self._service(cluster_id).activate(plan_id=plan_id, cluster_id=cluster_id)

    async def restore(self, *, plan_id: str, cluster_id: int):
        return await self._service(cluster_id).restore(plan_id=plan_id, cluster_id=cluster_id)

    async def aclose(self) -> None:
        if self._close_guard is None:
            return
        for cluster_id in sorted(self._services):
            result = self._close_guard(self._services[cluster_id])
            if inspect.isawaitable(result):
                await result
        self._services.clear()

    def _service(self, cluster_id: int):
        if cluster_id < 1:
            raise ValueError("Allocation guard cluster ID is invalid")
        service = self._services.get(cluster_id)
        if service is None:
            service = self._service_factory(cluster_id)
            self._services[cluster_id] = service
        return service
