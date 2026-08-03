from datetime import datetime, timedelta, timezone
import unittest

from pydantic import ValidationError

from app.maintenance_models import (
    AvailabilityMode,
    ClusterObservation,
    EvaluationStage,
    HostObservation,
    MaintenanceBackend,
    MaintenancePolicy,
    ObservationSnapshot,
    OperationKind,
    PlanningTarget,
    PredicateId,
    PredicateOutcome,
    ProviderType,
    RevisionObservation,
    SourceObservation,
    SourceStatus,
    WorkloadObservation,
)
from app.maintenance_safety import (
    HARD_PREDICATES,
    PREDICATE_ORDER,
    calculate_impact,
    evaluate_predicates,
)


NOW = datetime(2026, 8, 3, 4, 0, tzinfo=timezone.utc)


def workload(
    assignment_id,
    cluster_id,
    node_id,
    role,
    *,
    ready=True,
    master_eligible=False,
    data_tiers=(),
    endpoint_required=False,
):
    return WorkloadObservation(
        assignment_id=assignment_id,
        cluster_id=cluster_id,
        node_id=node_id,
        role=role,
        expected_running=True,
        running=ready,
        ready=ready,
        master_eligible=master_eligible,
        data_tiers=data_tiers,
        endpoint_required=endpoint_required,
        observed_at=NOW,
    )


def cluster(cluster_id, *, master_total=3, master_available=3, identity_matches=True):
    expected_uuid = f"cluster-{cluster_id}-uuid"
    return ClusterObservation(
        cluster_id=cluster_id,
        provider_type=ProviderType.NATIVE_PODMAN,
        backend=MaintenanceBackend.DOCUMENTED_ROLLING,
        lifecycle_supported=True,
        configured_name=f"cluster-{cluster_id}",
        configured_uuid=expected_uuid,
        observed_name=f"cluster-{cluster_id}",
        observed_uuid=expected_uuid if identity_matches else "wrong-cluster",
        health="green",
        master_eligible_total=master_total,
        master_eligible_available=master_available,
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
    )


def snapshot(*, clusters, workloads, hosts=None, conflicts=()):
    hosts = hosts or (
        HostObservation(
            node_id=1,
            enabled=True,
            initialized=True,
            reachable=True,
            membership_ready=True,
            observed_at=NOW,
        ),
    )
    return ObservationSnapshot(
        captured_at=NOW,
        capability_revision="capabilities-v1",
        sources=(
            SourceObservation(source="inventory", status=SourceStatus.OK, observed_at=NOW),
            SourceObservation(source="runtime", status=SourceStatus.OK, observed_at=NOW),
        ),
        hosts=hosts,
        clusters=clusters,
        workloads=workloads,
        assignment_revisions=tuple(
            RevisionObservation(assignment_id=item.assignment_id, revision=1)
            for item in workloads
        ),
        conflicting_operations=conflicts,
    )


class MaintenancePolicyTests(unittest.TestCase):
    def test_defaults_are_conservative_and_do_not_require_persistence(self):
        policy = MaintenancePolicy()

        self.assertEqual(policy.max_unavailable, 1)
        self.assertEqual(policy.minimum_master_eligible, "quorum")
        self.assertEqual(policy.minimum_data_per_tier, 1)
        self.assertEqual(policy.required_cluster_health, "green")
        self.assertEqual(policy.allocation_guard, "primaries-for-data")
        self.assertEqual(policy.max_surge, 0)

    def test_custom_policy_is_validated_and_normalized(self):
        policy = MaintenancePolicy(
            max_unavailable=2,
            minimum_master_eligible=2,
            required_cluster_health="yellow",
            restart_allocation_delay_seconds=60,
        )

        self.assertEqual(policy.minimum_master_eligible, 2)
        self.assertEqual(policy.restart_allocation_delay_seconds, 60)

    def test_impossible_or_unsafe_policy_values_are_rejected(self):
        for values in (
            {"max_unavailable": 0},
            {"minimum_master_eligible": 0},
            {"minimum_master_eligible": "all"},
            {"max_surge": 1},
            {"required_cluster_health": "red"},
            {"observation_max_age_seconds": 0},
        ):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                MaintenancePolicy(**values)


class MaintenanceImpactTests(unittest.TestCase):
    def test_host_impact_is_aggregated_across_every_cluster_on_the_host(self):
        workloads = (
            workload(11, 1, 1, "master", master_eligible=True, data_tiers=("hot",)),
            workload(12, 1, 2, "master", master_eligible=True, data_tiers=("hot",)),
            workload(13, 1, 3, "master", master_eligible=True, data_tiers=("hot",)),
            workload(14, 1, 1, "kibana", endpoint_required=True),
            workload(15, 1, 2, "kibana", endpoint_required=True),
            workload(21, 2, 1, "master", master_eligible=True, data_tiers=("warm",)),
        )
        observations = snapshot(
            clusters=(cluster(1), cluster(2, master_total=1, master_available=1)),
            workloads=workloads,
        )
        target = PlanningTarget(
            operation=OperationKind.REBOOT,
            node_id=1,
            reason="Kernel maintenance",
            availability_mode=AvailabilityMode.ZERO_IMPACT,
        )

        impact = calculate_impact(observations, target, MaintenancePolicy(max_unavailable=3))

        self.assertEqual(impact.affected_cluster_ids, (1, 2))
        self.assertEqual(impact.affected_assignment_ids, (11, 14, 21))
        first = impact.cluster(1)
        second = impact.cluster(2)
        self.assertEqual(first.master_available_after, 2)
        self.assertEqual(first.master_required, 2)
        self.assertTrue(first.master_quorum_safe)
        self.assertEqual(first.service("kibana").available_after, 1)
        self.assertFalse(second.master_quorum_safe)
        self.assertIn("master_quorum", second.violation_ids)

    def test_existing_unavailable_workloads_count_against_the_budget(self):
        workloads = (
            workload(11, 1, 1, "hot", data_tiers=("hot",)),
            workload(12, 1, 2, "warm", ready=False, data_tiers=("warm",)),
            workload(13, 1, 3, "warm", data_tiers=("warm",)),
        )
        observations = snapshot(clusters=(cluster(1),), workloads=workloads)
        target = PlanningTarget(operation=OperationKind.REBOOT, node_id=1)

        impact = calculate_impact(observations, target, MaintenancePolicy())

        item = impact.cluster(1)
        self.assertEqual(item.existing_unavailable, 1)
        self.assertEqual(item.planned_unavailable, 1)
        self.assertEqual(item.total_unavailable_after, 2)
        self.assertIn("max_unavailable", item.violation_ids)


class MaintenancePredicateTests(unittest.TestCase):
    def test_predicates_have_stable_order_and_hard_failures_cannot_be_forced(self):
        workloads = (
            workload(11, 1, 1, "master", master_eligible=True, data_tiers=("hot",)),
            workload(12, 1, 2, "master", master_eligible=True, data_tiers=("hot",)),
            workload(13, 1, 3, "master", master_eligible=True, data_tiers=("hot",)),
        )
        observations = snapshot(
            clusters=(cluster(1, identity_matches=False),),
            workloads=workloads,
        )
        target = PlanningTarget(operation=OperationKind.REBOOT, node_id=1)
        policy = MaintenancePolicy()
        impact = calculate_impact(observations, target, policy)

        results = evaluate_predicates(
            observations,
            target,
            policy,
            impact,
            now=NOW,
            override_ids={PredicateId.EXPECTED_CLUSTER_IDENTITY},
        )

        self.assertEqual(tuple(result.identifier for result in results), PREDICATE_ORDER)
        identity = next(result for result in results if result.identifier == PredicateId.EXPECTED_CLUSTER_IDENTITY)
        self.assertEqual(identity.outcome, PredicateOutcome.BLOCKED)
        self.assertFalse(identity.forceable)
        self.assertIn(PredicateId.EXPECTED_CLUSTER_IDENTITY, HARD_PREDICATES)

    def test_forceable_failure_becomes_an_explicit_warning(self):
        observations = snapshot(
            clusters=(cluster(1),),
            workloads=(workload(11, 1, 1, "hot", data_tiers=("hot",)),),
        )
        unsafe_cluster = observations.clusters[0].model_copy(update={"disk_watermarks_safe": False})
        observations = observations.model_copy(update={"clusters": (unsafe_cluster,)})
        target = PlanningTarget(operation=OperationKind.REBOOT, node_id=1)
        policy = MaintenancePolicy()
        impact = calculate_impact(observations, target, policy)

        results = evaluate_predicates(
            observations,
            target,
            policy,
            impact,
            now=NOW,
            override_ids={PredicateId.DISK_WATERMARKS_SAFE},
        )

        disk = next(result for result in results if result.identifier == PredicateId.DISK_WATERMARKS_SAFE)
        self.assertEqual(disk.outcome, PredicateOutcome.WARNING)
        self.assertTrue(disk.forceable)
        self.assertTrue(disk.override_applied)

    def test_stale_or_failed_required_sources_fail_fresh_runtime(self):
        observations = snapshot(
            clusters=(cluster(1),),
            workloads=(workload(11, 1, 1, "hot", data_tiers=("hot",)),),
        )
        stale_source = SourceObservation(
            source="runtime",
            status=SourceStatus.ERROR,
            observed_at=NOW - timedelta(minutes=10),
            error_category="timeout",
        )
        observations = observations.model_copy(update={"sources": (stale_source,)})
        target = PlanningTarget(operation=OperationKind.REBOOT, node_id=1)
        policy = MaintenancePolicy()
        impact = calculate_impact(observations, target, policy)

        results = evaluate_predicates(observations, target, policy, impact, now=NOW)

        fresh = next(result for result in results if result.identifier == PredicateId.FRESH_RUNTIME_OBSERVATION)
        self.assertEqual(fresh.outcome, PredicateOutcome.BLOCKED)
        self.assertNotIn("timeout", fresh.evidence_summary)

    def test_every_hard_predicate_stays_blocked_when_an_override_is_requested(self):
        workloads = (
            workload(11, 1, 1, "master", master_eligible=True, data_tiers=("hot",)),
        )
        unsafe = cluster(1, master_total=1, master_available=1, identity_matches=False).model_copy(update={
            "no_last_shard_copy": False,
            "primary_promotion_safe": False,
            "allocation_setting_captured": False,
            "version_transition_supported": False,
        })
        observations = snapshot(clusters=(unsafe,), workloads=workloads)
        target = PlanningTarget(
            operation=OperationKind.UPGRADE,
            cluster_id=1,
            current_version="8.19.0",
            target_version="9.0.0",
        )
        policy = MaintenancePolicy(max_unavailable=2)
        impact = calculate_impact(observations, target, policy)

        results = evaluate_predicates(
            observations,
            target,
            policy,
            impact,
            now=NOW,
            stage=EvaluationStage.PREFLIGHT,
            override_ids=set(HARD_PREDICATES),
        )

        by_id = {result.identifier: result for result in results}
        self.assertEqual(
            HARD_PREDICATES,
            {
                PredicateId.NO_LAST_SHARD_COPY,
                PredicateId.MASTER_QUORUM,
                PredicateId.EXPECTED_CLUSTER_IDENTITY,
                PredicateId.PRIMARY_PROMOTION_SAFETY,
                PredicateId.ALLOCATION_SETTING_CAPTURED,
                PredicateId.VERSION_TRANSITION_SUPPORTED,
            },
        )
        for identifier in HARD_PREDICATES:
            with self.subTest(identifier=identifier):
                self.assertEqual(by_id[identifier].outcome, PredicateOutcome.BLOCKED)
                self.assertFalse(by_id[identifier].forceable)
                self.assertFalse(by_id[identifier].override_applied)


if __name__ == "__main__":
    unittest.main()
