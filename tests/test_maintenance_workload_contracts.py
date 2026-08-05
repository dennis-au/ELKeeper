import unittest

from app.modules.maintenance.workload_contracts import (
    DisruptionBudget,
    ReadinessEvidence,
    WorkloadCheckpoint,
    WorkloadMaintenanceTarget,
    WorkloadOperation,
    WorkloadRole,
    rollback_allowed,
    legacy_role_to_workload_role,
    validate_readiness,
)


def target(*, role=WorkloadRole.KIBANA, digest=None):
    return WorkloadMaintenanceTarget(
        assignment_id=1,
        cluster_id=2,
        node_id=3,
        role=role,
        operation=WorkloadOperation.RESTART,
        expected_name="ecp-demo-kibana-1",
        expected_image="docker.elastic.co/kibana/kibana:8.19.1",
        expected_digest=digest,
        budget=DisruptionBudget(available_before=2, minimum_ready=1),
    )


class WorkloadContractTests(unittest.TestCase):
    def test_budget_blocks_when_singleton_would_be_unavailable(self):
        budget = DisruptionBudget(available_before=1, minimum_ready=1)
        self.assertFalse(budget.safe)
        self.assertEqual(budget.reason, "minimum_ready_budget_not_met")

    def test_readiness_requires_identity_and_digest(self):
        item = target(digest="sha256:" + "a" * 64)
        evidence = ReadinessEvidence(
            ready=True,
            observed_at="2026-08-04T00:00:00Z",
            identity_matches=True,
            image_digest="sha256:" + "b" * 64,
        )
        self.assertEqual(validate_readiness(item, evidence), (False, "workload_digest_mismatch"))

    def test_ready_checkpoint_requires_ready_evidence(self):
        with self.assertRaises(ValueError):
            WorkloadCheckpoint(sequence=1, target=target(), state="ready")

    def test_elasticsearch_cannot_auto_downgrade_after_process_start(self):
        self.assertFalse(rollback_allowed(WorkloadRole.ELASTICSEARCH, process_started=True))
        self.assertTrue(rollback_allowed(WorkloadRole.KIBANA, process_started=True))

    def test_legacy_role_aliases_are_normalized_before_rollback_policy(self):
        self.assertEqual(legacy_role_to_workload_role("hot"), WorkloadRole.ELASTICSEARCH)
        self.assertEqual(legacy_role_to_workload_role("fleet"), WorkloadRole.FLEET_SERVER)


if __name__ == "__main__":
    unittest.main()
