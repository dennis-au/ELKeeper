from __future__ import annotations

import unittest
from datetime import datetime, timezone

from pydantic import TypeAdapter, ValidationError

from app.modules.maintenance.contracts import (
    AllocationGuardStatus,
    MaintenanceActionAvailability,
    MaintenanceTarget,
    MaintenanceTargetScope,
    MaintenanceWorkflowAction,
    MaintenanceWorkflowState,
    MaintenanceWorkflowSummary,
)


NOW = datetime(2026, 8, 5, 0, 0, tzinfo=timezone.utc)


class PlannedMaintenanceContractTests(unittest.TestCase):
    def test_public_target_union_distinguishes_host_and_managed_container(self):
        adapter = TypeAdapter(MaintenanceTarget)
        host = adapter.validate_python({"scope": "host", "node_id": 11})
        container = adapter.validate_python({"scope": "container", "assignment_id": 22})

        self.assertEqual(host.scope, MaintenanceTargetScope.HOST)
        self.assertEqual(host.node_id, 11)
        self.assertEqual(container.scope, MaintenanceTargetScope.CONTAINER)
        self.assertEqual(container.assignment_id, 22)

        with self.assertRaises(ValidationError):
            adapter.validate_python({"scope": "host", "assignment_id": 22})
        with self.assertRaises(ValidationError):
            adapter.validate_python({"scope": "container", "node_id": 11})

    def test_summary_exposes_scope_impact_guard_checkpoint_and_actions(self):
        summary = MaintenanceWorkflowSummary.model_validate({
            "state": "ready_to_stop",
            "target": {"scope": "container", "assignment_id": 22},
            "affected_workloads": [{
                "assignment_id": 22,
                "cluster_id": 7,
                "node_id": 11,
                "role": "hot",
                "name": "es-hot-1",
            }],
            "affected_clusters": [{
                "cluster_id": 7,
                "name": "logs-a",
                "data_node_affected": True,
            }],
            "preflight": [{
                "identifier": "MasterQuorum",
                "outcome": "passed",
                "summary": "Two master-eligible nodes remain available.",
                "observed_at": NOW,
            }],
            "allocation_guards": [{
                "cluster_id": 7,
                "owner_plan_id": "plan-22",
                "phase": "active",
                "captured_persistent": "all",
                "captured_transient": None,
                "observed_effective": "primaries",
                "updated_at": NOW,
            }],
            "checkpoints": [{
                "sequence": 2,
                "key": "container.allocation-guard-active",
                "state": "verified",
                "summary": "Replica allocation is paused for the planned restart.",
                "observed_at": NOW,
            }],
            "actions": [
                {"action": "prepare", "enabled": False, "reason": "Already prepared."},
                {"action": "stop", "enabled": True},
                {"action": "return", "enabled": False, "reason": "Target has not stopped."},
                {"action": "recover", "enabled": False, "reason": "No recovery is required."},
            ],
        })

        self.assertEqual(summary.state, MaintenanceWorkflowState.READY_TO_STOP)
        self.assertEqual(summary.target.scope, MaintenanceTargetScope.CONTAINER)
        self.assertEqual(summary.affected_workloads[0].assignment_id, 22)
        self.assertTrue(summary.affected_clusters[0].data_node_affected)
        self.assertEqual(summary.allocation_guards[0].phase, "active")
        self.assertEqual(summary.actions[1], MaintenanceActionAvailability(
            action=MaintenanceWorkflowAction.STOP,
            enabled=True,
        ))

    def test_summary_rejects_duplicate_cluster_guards_and_actions(self):
        base = {
            "state": "available",
            "target": {"scope": "host", "node_id": 11},
        }
        with self.assertRaises(ValidationError):
            MaintenanceWorkflowSummary.model_validate({
                **base,
                "allocation_guards": [
                    {"cluster_id": 7, "owner_plan_id": "plan-a", "phase": "captured", "updated_at": NOW},
                    {"cluster_id": 7, "owner_plan_id": "plan-b", "phase": "active", "updated_at": NOW},
                ],
            })
        with self.assertRaises(ValidationError):
            MaintenanceWorkflowSummary.model_validate({
                **base,
                "actions": [
                    {"action": "prepare", "enabled": True},
                    {"action": "prepare", "enabled": False, "reason": "Duplicate."},
                ],
            })

    def test_allocation_guard_requires_an_owner_when_not_restored(self):
        with self.assertRaises(ValidationError):
            AllocationGuardStatus(
                cluster_id=7,
                owner_plan_id="",
                phase="active",
                updated_at=NOW,
            )


if __name__ == "__main__":
    unittest.main()
