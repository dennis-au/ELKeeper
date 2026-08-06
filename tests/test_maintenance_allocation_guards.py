from __future__ import annotations

import unittest
from datetime import timedelta

from app.modules.maintenance.allocation_guards import (
    AllocationGuardConflict,
    AllocationGuardOwnershipError,
    AllocationGuardService,
    ClusterAllocationGuardRouter,
)
from app.modules.maintenance.elasticsearch import (
    AllocationCleanupTrigger,
    AllocationGuardApplyResult,
    AllocationGuardCheckpoint,
    AllocationGuardCleanupResult,
    AllocationGuardPhase,
    AllocationSettingCapture,
    SettingLayerValue,
)
from app.modules.maintenance.lifecycle import MaintenanceState
from app.modules.maintenance.store import MaintenanceRepository, install_maintenance_schema
from tests.test_maintenance_store import NOW, base_connection


def checkpoint(plan_id: str, cluster_id: int, phase: AllocationGuardPhase) -> AllocationGuardCheckpoint:
    capture = AllocationSettingCapture(
        persistent=SettingLayerValue(present=True, value="all"),
        transient=SettingLayerValue(present=False),
        captured_at=NOW,
    )
    observed = capture if phase == AllocationGuardPhase.CAPTURED else AllocationSettingCapture(
        persistent=SettingLayerValue(present=True, value="primaries"),
        transient=SettingLayerValue(present=False),
        captured_at=NOW,
    )
    return AllocationGuardCheckpoint(
        plan_id=plan_id,
        cluster_id=cluster_id,
        phase=phase,
        captured=capture,
        observed=observed,
        updated_at=NOW,
    )


class FakeAllocationController:
    def __init__(self):
        self.calls: list[tuple[str, str, int]] = []

    async def capture(self, *, plan_id: str, cluster_id: int) -> AllocationGuardCheckpoint:
        self.calls.append(("capture", plan_id, cluster_id))
        return checkpoint(plan_id, cluster_id, AllocationGuardPhase.CAPTURED)

    async def activate(self, value: AllocationGuardCheckpoint) -> AllocationGuardApplyResult:
        self.calls.append(("activate", value.plan_id, value.cluster_id))
        active = checkpoint(value.plan_id, value.cluster_id, AllocationGuardPhase.ACTIVE)
        return AllocationGuardApplyResult(status="active", checkpoint=active)

    async def restore(
        self,
        value: AllocationGuardCheckpoint,
        *,
        trigger: AllocationCleanupTrigger,
    ) -> AllocationGuardCleanupResult:
        self.calls.append(("restore", value.plan_id, value.cluster_id))
        restored = checkpoint(value.plan_id, value.cluster_id, AllocationGuardPhase.RESTORED)
        return AllocationGuardCleanupResult(
            status="restored",
            trigger=trigger,
            verified=True,
            checkpoint=restored,
        )


class AllocationGuardServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.connection = base_connection()
        install_maintenance_schema(self.connection)
        self.repository = MaintenanceRepository(self.connection)
        self.addCleanup(self.connection.close)

    def plan(self, value: str):
        return self.repository.create_plan(
            plan_id=value * 32,
            operation_kind="reboot",
            plan={"target": value},
            observation={},
            idempotency_key=f"guard-{value}",
            requested_by="operator",
            expires_at=NOW + timedelta(minutes=5),
            initial_state=MaintenanceState.READY,
        )

    async def test_guard_owner_blocks_competing_plan_and_restores_exact_owned_checkpoint(self):
        first = self.plan("a")
        second = self.plan("b")
        controller = FakeAllocationController()
        service = AllocationGuardService(self.repository, controller)

        captured = await service.capture(plan_id=first.id, cluster_id=1)
        self.assertEqual(captured.phase, AllocationGuardPhase.CAPTURED)
        with self.assertRaises(AllocationGuardConflict):
            await service.capture(plan_id=second.id, cluster_id=1)

        active = await service.activate(plan_id=first.id, cluster_id=1)
        self.assertEqual(active.phase, AllocationGuardPhase.ACTIVE)
        with self.assertRaises(AllocationGuardOwnershipError):
            await service.restore(
                plan_id=second.id,
                cluster_id=1,
                trigger=AllocationCleanupTrigger.RECOVERY,
            )

        restored = await service.restore(
            plan_id=first.id,
            cluster_id=1,
            trigger=AllocationCleanupTrigger.SUCCESS,
        )
        self.assertEqual(restored.phase, AllocationGuardPhase.RESTORED)
        replacement = await service.capture(plan_id=second.id, cluster_id=1)
        self.assertEqual(replacement.plan_id, second.id)

    async def test_restarted_service_recovers_the_persisted_active_owner(self):
        plan = self.plan("c")
        controller = FakeAllocationController()
        service = AllocationGuardService(self.repository, controller)
        await service.capture(plan_id=plan.id, cluster_id=1)
        await service.activate(plan_id=plan.id, cluster_id=1)

        restarted = AllocationGuardService(self.repository, controller)
        restored = await restarted.restore(
            plan_id=plan.id,
            cluster_id=1,
            trigger=AllocationCleanupTrigger.RECOVERY,
        )

        self.assertEqual(restored.phase, AllocationGuardPhase.RESTORED)
        self.assertIn(("restore", plan.id, 1), controller.calls)


class ClusterAllocationGuardRouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_router_keeps_one_guard_service_per_cluster_and_closes_all_created_clients(self):
        created = []
        closed = []

        class Guard:
            def __init__(self, cluster_id):
                self.cluster_id = cluster_id
                self.calls = []

            async def capture(self, *, plan_id, cluster_id):
                self.calls.append(("capture", plan_id, cluster_id))
                return cluster_id

            async def activate(self, *, plan_id, cluster_id):
                self.calls.append(("activate", plan_id, cluster_id))
                return cluster_id

            async def restore(self, *, plan_id, cluster_id):
                self.calls.append(("restore", plan_id, cluster_id))
                return cluster_id

        def factory(cluster_id):
            guard = Guard(cluster_id)
            created.append(guard)
            return guard

        async def close(guard):
            closed.append(guard.cluster_id)

        router = ClusterAllocationGuardRouter(factory, close_guard=close)
        await router.capture(plan_id="a" * 32, cluster_id=1)
        await router.activate(plan_id="a" * 32, cluster_id=1)
        await router.capture(plan_id="b" * 32, cluster_id=2)
        await router.restore(plan_id="a" * 32, cluster_id=1)
        await router.aclose()

        self.assertEqual(len(created), 2)
        self.assertEqual(created[0].calls, [
            ("capture", "a" * 32, 1),
            ("activate", "a" * 32, 1),
            ("restore", "a" * 32, 1),
        ])
        self.assertEqual(closed, [1, 2])


if __name__ == "__main__":
    unittest.main()
