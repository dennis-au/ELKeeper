from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone

from app.modules.maintenance.contracts import (
    DisruptionBudget,
    LegacyWorkloadObservation,
    ReadinessEvidence,
    WorkloadMaintenancePlanInput,
    WorkloadMaintenanceTarget,
    WorkloadOperation,
    WorkloadRole,
    classify_legacy_batch_recovery,
)
from app.modules.maintenance import (
    WorkloadMaintenancePlanService,
    workload_maintenance_progress_in_connection,
)
from app.modules.maintenance.store import MaintenanceRepository, install_maintenance_schema
from app.modules.workloads.worker import WorkloadChangeWorker
from tests.test_maintenance_store import base_connection


NOW = datetime(2026, 8, 4, 0, 0, tzinfo=timezone.utc)


def target(*, role: WorkloadRole = WorkloadRole.KIBANA) -> WorkloadMaintenanceTarget:
    return WorkloadMaintenanceTarget(
        assignment_id=1,
        cluster_id=1,
        node_id=1,
        role=role,
        operation=WorkloadOperation.RESTART,
        expected_name="ecp-alpha-workload-1",
        expected_image="docker.elastic.co/kibana/kibana:8.19.1",
        budget=DisruptionBudget(available_before=2, minimum_ready=1),
    )


class WorkloadMaintenancePlanServiceTests(unittest.TestCase):
    def setUp(self):
        self.connection = base_connection()
        install_maintenance_schema(self.connection)
        self.repository = MaintenanceRepository(self.connection)
        self.service = WorkloadMaintenancePlanService(self.repository, clock=lambda: NOW)

    def tearDown(self):
        self.connection.close()

    def request(self, *, role=WorkloadRole.KIBANA, key="workload-plan-1") -> WorkloadMaintenancePlanInput:
        return WorkloadMaintenancePlanInput(
            target=target(role=role),
            reason="planned maintenance",
            idempotency_key=key,
        )

    def test_preview_is_persisted_but_execution_is_fail_closed(self):
        response = self.service.create_preview(self.request(), requested_by="operator")

        self.assertEqual(response["lifecycle_state"], "blocked")
        self.assertFalse(response["execution_enabled"])
        self.assertIn("rolling_restart_capability_disabled", response["execution_blockers"])
        self.assertEqual(response["step_count"], 6)
        self.assertEqual(response["checkpoint"]["side_effect_state"], "not_started")
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 2)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM maintenance_locks").fetchone()[0], 0)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0], 1)

    def test_preview_idempotency_does_not_duplicate_checkpoints_or_audit(self):
        first = self.service.create_preview(self.request(), requested_by="operator")
        second = self.service.create_preview(self.request(), requested_by="operator")

        self.assertEqual(first["plan_id"], second["plan_id"])
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM maintenance_checkpoints").fetchone()[0], 1)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0], 1)

    def test_assignment_progress_projection_exposes_only_workload_plans(self):
        preview = self.service.create_preview(self.request(), requested_by="operator")
        self.repository.create_plan(
            operation_kind="reboot",
            plan={"kind": "host_maintenance"},
            idempotency_key="host-plan-1",
            requested_by="operator",
            expires_at=NOW + timedelta(minutes=5),
            target_node_id=1,
            initial_state="blocked",
        )

        projection = self.service.progress_for_assignments((1, 2))

        self.assertEqual(set(projection), {1})
        self.assertEqual(projection[1]["plan_id"], preview["plan_id"])

    def test_public_progress_projection_uses_only_the_active_connection(self):
        preview = self.service.create_preview(self.request(), requested_by="operator")

        projection = workload_maintenance_progress_in_connection(self.connection, (1,))

        self.assertEqual(projection[1]["plan_id"], preview["plan_id"])

    def test_elasticsearch_failure_after_process_start_requires_recovery(self):
        preview = self.service.create_preview(self.request(role=WorkloadRole.ELASTICSEARCH), requested_by="operator")
        result = self.service.observe_checkpoint(
            preview["plan_id"],
            ReadinessEvidence(ready=False, observed_at="2026-08-04T00:00:01Z", identity_matches=True),
            process_started=True,
        )

        self.assertEqual(result["checkpoint"]["recovery_classification"], "recovery_required")
        self.assertEqual(result["checkpoint"]["recovery_reason"], "elasticsearch_no_automatic_downgrade")
        self.assertFalse(result["checkpoint"]["resumable"])

    def test_stateless_failed_readiness_is_resumable_not_auto_downgrade_blocked(self):
        preview = self.service.create_preview(self.request(), requested_by="operator")
        result = self.service.observe_checkpoint(
            preview["plan_id"],
            ReadinessEvidence(ready=False, observed_at="2026-08-04T00:00:01Z", identity_matches=True),
            process_started=True,
        )

        self.assertEqual(result["checkpoint"]["recovery_classification"], "incomplete")
        self.assertTrue(result["checkpoint"]["resumable"])


class LegacyBatchRecoveryClassificationTests(unittest.TestCase):
    def test_missing_runtime_observation_fails_closed(self):
        decision = classify_legacy_batch_recovery(
            [{"assignment_id": 1, "role": "kibana"}],
            (),
        )
        self.assertEqual(decision.classification, "no_observation")

    def test_elasticsearch_process_started_requires_operator_recovery(self):
        decision = classify_legacy_batch_recovery(
            [{"assignment_id": 1, "role": "hot"}],
            (
                LegacyWorkloadObservation(
                    assignment_id=1,
                    role=WorkloadRole.ELASTICSEARCH,
                    process_started=True,
                    identity_matches=True,
                    ready=False,
                ),
            ),
        )
        self.assertEqual(decision.classification, "recovery_required")
        self.assertEqual(decision.reason, "elasticsearch_process_may_have_opened_data")

    def test_observed_stateless_assignment_is_the_only_rollback_candidate(self):
        decision = classify_legacy_batch_recovery(
            [{"assignment_id": 1, "role": "kibana"}],
            (
                LegacyWorkloadObservation(
                    assignment_id=1,
                    role=WorkloadRole.KIBANA,
                    process_started=True,
                    identity_matches=True,
                    ready=False,
                ),
            ),
        )
        self.assertEqual(decision.classification, "rollback")
        self.assertEqual(decision.rollback_assignment_ids, (1,))

    def test_legacy_worker_uses_an_injected_observation_projection(self):
        decision = classify_legacy_batch_recovery(
            [{"assignment_id": 1, "role": "kibana"}],
            (
                LegacyWorkloadObservation(
                    assignment_id=1,
                    role=WorkloadRole.KIBANA,
                    process_started=True,
                    identity_matches=True,
                    ready=False,
                ),
            ),
        )
        worker = object.__new__(WorkloadChangeWorker)
        worker._batch_recovery_observer = lambda *_: decision

        observed = asyncio.run(worker._observe_batch_recovery(7, {"changes": []}, []))

        self.assertIs(observed, decision)


if __name__ == "__main__":
    unittest.main()
