from datetime import datetime, timedelta, timezone
import json
import unittest

from app.maintenance_models import (
    AvailabilityMode,
    ClusterObservation,
    HostObservation,
    MaintenanceBackend,
    MaintenancePolicy,
    ObservationSnapshot,
    OperationKind,
    PlanStep,
    PlanningTarget,
    PredicateId,
    PredicateOutcome,
    PredicateResult,
    PredicateSeverity,
    ProviderType,
    RevisionObservation,
    RollbackBoundary,
    SourceObservation,
    SourceStatus,
    WorkloadObservation,
)
from app.maintenance_planning import (
    canonical_json,
    compile_plan,
    validate_plan_for_execution,
    verify_plan_hash,
)
from app.maintenance_safety import calculate_impact


NOW = datetime(2026, 8, 3, 5, 0, tzinfo=timezone.utc)


def observations(source_order=("runtime", "inventory")):
    return ObservationSnapshot(
        captured_at=NOW,
        capability_revision="cap-v1",
        sources=tuple(
            SourceObservation(source=name, status=SourceStatus.OK, observed_at=NOW)
            for name in source_order
        ),
        hosts=(
            HostObservation(
                node_id=7,
                enabled=True,
                initialized=True,
                reachable=True,
                membership_ready=True,
                observed_at=NOW,
            ),
        ),
        clusters=(
            ClusterObservation(
                cluster_id=9,
                provider_type=ProviderType.NATIVE_PODMAN,
                backend=MaintenanceBackend.DOCUMENTED_ROLLING,
                lifecycle_supported=True,
                configured_name="alpha",
                configured_uuid="uuid-alpha",
                observed_name="alpha",
                observed_uuid="uuid-alpha",
                health="green",
                master_eligible_total=3,
                master_eligible_available=3,
                initializing_shards=0,
                relocating_shards=0,
                no_last_shard_copy=True,
                primary_promotion_safe=True,
                allocation_setting_captured=True,
                disk_watermarks_safe=True,
                target_artifact_ready=True,
                version_transition_supported=True,
                snapshot_recovery_ready=True,
                stale_shutdown_record=False,
                observed_at=NOW,
            ),
        ),
        workloads=(
            WorkloadObservation(
                assignment_id=31,
                cluster_id=9,
                node_id=7,
                role="hot",
                expected_running=True,
                running=True,
                ready=True,
                master_eligible=False,
                data_tiers=("hot",),
                endpoint_required=False,
                observed_at=NOW,
            ),
        ),
        assignment_revisions=(RevisionObservation(assignment_id=31, revision=4),),
    )


def predicates():
    return (
        PredicateResult(
            identifier=PredicateId.HOST_ENABLED,
            severity=PredicateSeverity.INFO,
            outcome=PredicateOutcome.PASSED,
            applicable=True,
            forceable=True,
            override_applied=False,
            evidence_summary="Host 7 is enabled and initialized.",
            remediation="",
            observed_at=NOW,
        ),
    )


class MaintenanceCanonicalizationTests(unittest.TestCase):
    def test_canonical_json_sorts_keys_and_normalizes_utc_datetimes(self):
        encoded = canonical_json({"z": 1, "when": NOW, "a": {"two": 2, "one": 1}})

        self.assertEqual(
            encoded,
            '{"a":{"one":1,"two":2},"when":"2026-08-03T05:00:00Z","z":1}',
        )

    def test_observation_snapshot_normalizes_unordered_collections(self):
        first = observations(("runtime", "inventory"))
        second = observations(("inventory", "runtime"))

        self.assertEqual(canonical_json(first), canonical_json(second))


class MaintenancePlanCompilerTests(unittest.TestCase):
    def compile(self, *, policy=None, observed=None):
        policy = policy or MaintenancePolicy()
        observed = observed or observations()
        target = PlanningTarget(
            operation=OperationKind.REBOOT,
            node_id=7,
            reason="Operating-system maintenance",
            availability_mode=AvailabilityMode.ZERO_IMPACT,
        )
        impact = calculate_impact(observed, target, policy)
        return compile_plan(
            target=target,
            policy=policy,
            policy_revision=3,
            backend=MaintenanceBackend.DOCUMENTED_ROLLING,
            observation=observed,
            predicates=predicates(),
            impact=impact,
            steps=(
                PlanStep(sequence=1, kind="refresh-observations", summary="Refresh safety observations"),
                PlanStep(sequence=2, kind="reboot-host", summary="Reboot the selected host"),
            ),
            rollback_boundaries=(
                RollbackBoundary(
                    before_step=2,
                    behavior="Abort before the host reboot side effect.",
                ),
            ),
            created_at=NOW,
            idempotency_key="maintenance-request-1",
        )

    def test_same_semantic_input_produces_the_same_hash(self):
        first = self.compile(observed=observations(("runtime", "inventory")))
        second = self.compile(observed=observations(("inventory", "runtime")))

        self.assertEqual(first.plan_hash, second.plan_hash)
        self.assertTrue(verify_plan_hash(first))
        self.assertEqual(first.expires_at, NOW + timedelta(seconds=300))

    def test_plan_serialization_contains_no_secret_shaped_fields(self):
        plan = self.compile()
        document = json.loads(canonical_json(plan))
        serialized = canonical_json(document).lower()

        self.assertNotIn("password", serialized)
        self.assertNotIn("private_key", serialized)
        self.assertNotIn("api_key", serialized)
        self.assertNotIn("token", serialized)

    def test_execution_validation_fails_closed_for_expiry_and_changed_revisions(self):
        plan = self.compile()
        current_revisions = (RevisionObservation(assignment_id=31, revision=5),)

        validation = validate_plan_for_execution(
            plan,
            now=NOW + timedelta(minutes=6),
            expected_plan_hash=plan.plan_hash,
            current_policy_revision=4,
            current_capability_revision="cap-v2",
            current_assignment_revisions=current_revisions,
        )

        self.assertFalse(validation.valid)
        self.assertEqual(
            set(validation.issue_codes),
            {
                "plan_expired",
                "policy_revision_changed",
                "capability_revision_changed",
                "assignment_revision_changed",
                "stale_observation",
            },
        )

    def test_execution_validation_rejects_a_tampered_or_blocked_plan(self):
        plan = self.compile()
        blocked = predicates()[0].model_copy(
            update={
                "outcome": PredicateOutcome.BLOCKED,
                "severity": PredicateSeverity.CRITICAL,
            }
        )
        tampered = plan.model_copy(update={"predicates": (blocked,)})

        validation = validate_plan_for_execution(
            tampered,
            now=NOW,
            expected_plan_hash=plan.plan_hash,
            current_policy_revision=3,
            current_capability_revision="cap-v1",
            current_assignment_revisions=(RevisionObservation(assignment_id=31, revision=4),),
        )

        self.assertFalse(validation.valid)
        self.assertIn("plan_hash_mismatch", validation.issue_codes)
        self.assertIn("blocking_predicate", validation.issue_codes)


if __name__ == "__main__":
    unittest.main()
