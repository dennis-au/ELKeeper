from datetime import datetime, timedelta, timezone
import json
import sqlite3
import unittest

from app.maintenance_lifecycle import MaintenanceState
from app.maintenance_models import (
    ClusterObservation,
    CompiledPlan,
    HostObservation,
    HostMaintenancePreviewRequest,
    MaintenanceBackend,
    ProviderType,
    RevisionObservation,
    SourceObservation,
    SourceStatus,
    WorkloadObservation,
)
from app.modules.maintenance.post_return import (
    ClusterExpectation,
    EndpointExpectation,
    NodeIdentityExpectation,
    PostReturnExpectations,
    ServiceBudgetExpectation,
)
from app.maintenance_planning import verify_plan_hash
from app.maintenance_service import (
    HostRebootPlanRequest,
    HostRebootPlanningData,
    MaintenancePlanningService,
    build_host_reboot_snapshot,
)
from app.modules.maintenance.store import (
    IdempotencyConflict,
    MaintenanceRepository,
    install_maintenance_schema,
)


NOW = datetime(2026, 8, 3, 8, 0, tzinfo=timezone.utc)


def connection():
    value = sqlite3.connect(":memory:")
    value.row_factory = sqlite3.Row
    value.execute("PRAGMA foreign_keys = ON")
    value.executescript("""
        CREATE TABLE nodes (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL);
        CREATE TABLE clusters (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL);
        CREATE TABLE runs (id INTEGER PRIMARY KEY, status TEXT NOT NULL);
        CREATE TABLE cluster_assignments (
          id INTEGER PRIMARY KEY,
          cluster_id INTEGER NOT NULL REFERENCES clusters(id),
          node_id INTEGER NOT NULL REFERENCES nodes(id),
          revision INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE audit_events (
          id INTEGER PRIMARY KEY,
          username TEXT NOT NULL,
          action TEXT NOT NULL,
          cluster_id INTEGER,
          item_id TEXT NOT NULL DEFAULT '',
          detail TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO nodes(id,name) VALUES(1,'node-a'),(2,'node-b'),(3,'node-c');
        INSERT INTO clusters(id,name) VALUES(1,'cluster-a'),(2,'cluster-b');
        INSERT INTO cluster_assignments(id,cluster_id,node_id,revision) VALUES
          (11,1,1,4),(12,1,2,2),(13,1,3,1),(14,1,1,1),
          (21,2,1,3),(22,2,2,1),(23,2,3,1);
    """)
    install_maintenance_schema(value)
    return value


def host(node_id):
    return HostObservation(
        node_id=node_id,
        name=f"node-{chr(96 + node_id)}",
        enabled=True,
        initialized=True,
        reachable=True,
        membership_ready=True,
        observed_at=NOW,
    )


def cluster(cluster_id):
    return ClusterObservation(
        cluster_id=cluster_id,
        provider_type=ProviderType.NATIVE_PODMAN,
        backend=MaintenanceBackend.DOCUMENTED_ROLLING,
        lifecycle_supported=True,
        configured_name=f"cluster-{cluster_id}",
        configured_uuid=f"uuid-{cluster_id}",
        observed_name=f"cluster-{cluster_id}",
        observed_uuid=f"uuid-{cluster_id}",
        health="green",
        master_eligible_total=3,
        master_eligible_available=3,
        initializing_shards=0,
        relocating_shards=0,
        no_last_shard_copy=True,
        primary_promotion_safe=True,
        allocation_setting_captured=False,
        disk_watermarks_safe=True,
        target_artifact_ready=True,
        version_transition_supported=True,
        snapshot_recovery_ready=True,
        stale_shutdown_record=False,
        observed_at=NOW,
    )


def master(assignment_id, cluster_id, node_id, revision):
    return WorkloadObservation(
        assignment_id=assignment_id,
        cluster_id=cluster_id,
        node_id=node_id,
        role="master",
        expected_running=True,
        running=True,
        ready=True,
        master_eligible=True,
        data_tiers=("hot",),
        endpoint_required=False,
        observed_at=NOW,
    ), RevisionObservation(assignment_id=assignment_id, revision=revision)


def post_return_expectations():
    return PostReturnExpectations(
        endpoints=(EndpointExpectation(endpoint_ref="kibana-status"),),
        clusters=(
            ClusterExpectation(
                cluster_id=1,
                required_health="green",
                nodes=(
                    NodeIdentityExpectation(
                        cluster_id=1,
                        assignment_id=11,
                        persistent_node_id="persistent-node-1",
                        node_name="ecp-cluster-a-master-1",
                        version="8.19.0",
                        cluster_uuid="cluster_uuid_1",
                    ),
                ),
            ),
            ClusterExpectation(
                cluster_id=2,
                required_health="green",
                nodes=(
                    NodeIdentityExpectation(
                        cluster_id=2,
                        assignment_id=21,
                        persistent_node_id="persistent-node-2",
                        node_name="ecp-cluster-b-master-1",
                        version="8.19.0",
                        cluster_uuid="cluster_uuid_2",
                    ),
                ),
            ),
        ),
        service_budgets=(
            ServiceBudgetExpectation(cluster_id=1, role="master", minimum_available=2),
            ServiceBudgetExpectation(cluster_id=2, role="master", minimum_available=2),
        ),
    )


def planning_data(*, captured_at=NOW, singleton_kibana=False, post_return=None):
    workload_pairs = (
        master(11, 1, 1, 4),
        master(12, 1, 2, 2),
        master(13, 1, 3, 1),
        master(21, 2, 1, 3),
        master(22, 2, 2, 1),
        master(23, 2, 3, 1),
    )
    workloads = [item[0] for item in workload_pairs]
    revisions = [item[1] for item in workload_pairs]
    if singleton_kibana:
        workloads.append(WorkloadObservation(
            assignment_id=14,
            cluster_id=1,
            node_id=1,
            role="kibana",
            expected_running=True,
            running=True,
            ready=True,
            endpoint_required=True,
            observed_at=NOW,
        ))
        revisions.append(RevisionObservation(assignment_id=14, revision=1))
    return HostRebootPlanningData(
        target_node_id=1,
        captured_at=captured_at,
        capability_revision="cap-v1",
        sources=(
            SourceObservation(source="inventory", status=SourceStatus.OK, observed_at=NOW),
            SourceObservation(source="runtime", status=SourceStatus.OK, observed_at=NOW),
            SourceObservation(source="elasticsearch", status=SourceStatus.OK, observed_at=NOW),
        ),
        hosts=(host(1), host(2), host(3)),
        clusters=(cluster(1), cluster(2)),
        workloads=tuple(workloads),
        assignment_revisions=tuple(revisions),
        post_return_expectations=post_return,
    )


class HostRebootSnapshotTests(unittest.TestCase):
    def test_builder_produces_an_immutable_target_scoped_snapshot(self):
        data = planning_data()

        snapshot = build_host_reboot_snapshot(data)

        self.assertEqual(snapshot.captured_at, NOW)
        self.assertEqual(tuple(item.cluster_id for item in snapshot.clusters), (1, 2))
        self.assertEqual(tuple(item.assignment_id for item in snapshot.workloads), (11, 12, 13, 21, 22, 23))
        self.assertEqual(snapshot.capability_revision, "cap-v1")

    def test_builder_rejects_a_missing_target_host(self):
        data = planning_data().model_copy(update={"target_node_id": 99})

        with self.assertRaisesRegex(ValueError, "target host observation"):
            build_host_reboot_snapshot(data)


class MaintenancePlanningServiceTests(unittest.TestCase):
    def setUp(self):
        self.connection = connection()
        self.repository = MaintenanceRepository(self.connection)
        self.service = MaintenancePlanningService(self.repository, clock=lambda: NOW)

    def tearDown(self):
        self.connection.close()

    def request(self, **changes):
        values = {
            "reason": "Operating-system maintenance",
            "availability_mode": "zero-impact",
            "idempotency_key": "host-1-reboot-request",
        }
        values.update(changes)
        return HostRebootPlanRequest(**values)

    def test_ready_preview_persists_the_compiler_hash_without_locks_or_remote_state(self):
        preview = self.service.create_host_reboot_preview(
            planning_data(),
            self.request(),
            requested_by="operator",
        )

        self.assertEqual(preview["lifecycle_state"], "ready")
        stored = self.repository.get_plan(preview["plan_id"])
        compiled = CompiledPlan.model_validate(stored.plan)
        self.assertEqual(stored.plan_hash, compiled.plan_hash)
        self.assertEqual(preview["plan_hash"], compiled.plan_hash)
        self.assertTrue(verify_plan_hash(compiled))
        self.assertTrue(self.repository.verify_plan_hash(stored.id, compiled.plan_hash))
        self.assertEqual(len(self.repository.list_steps(stored.id)), len(compiled.steps))
        self.assertEqual(self.repository.list_active_locks(), [])
        self.assertEqual(self.repository.get_host_state(1).state.value, "available")

    def test_singleton_service_is_persisted_as_a_blocked_preview(self):
        preview = self.service.create_host_reboot_preview(
            planning_data(singleton_kibana=True),
            self.request(idempotency_key="blocked-request"),
            requested_by="operator",
        )

        self.assertEqual(preview["lifecycle_state"], "blocked")
        blockers = {item["id"] for item in preview["view"]["predicates"] if item["outcome"] == "blocking"}
        self.assertIn("RoleAvailabilityBudget", blockers)
        self.assertEqual(preview["view"]["impact"]["endpoints"][0]["availability"], "unavailable")
        self.assertFalse(preview["view"]["execution_enabled"])

    def test_repeated_idempotency_key_returns_the_original_preview_without_replanning(self):
        first = self.service.create_host_reboot_preview(
            planning_data(),
            self.request(),
            requested_by="operator",
        )
        later_data = planning_data(captured_at=NOW + timedelta(minutes=1))
        second = self.service.create_host_reboot_preview(
            later_data,
            self.request(),
            requested_by="operator",
        )

        self.assertEqual(second["plan_id"], first["plan_id"])
        self.assertEqual(second["plan_hash"], first["plan_hash"])
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM maintenance_plans").fetchone()[0],
            1,
        )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM audit_events WHERE action='maintenance-plan-preview-created'").fetchone()[0],
            1,
        )

    def test_reusing_an_idempotency_key_for_a_different_request_is_rejected(self):
        self.service.create_host_reboot_preview(planning_data(), self.request(), requested_by="operator")

        with self.assertRaises(IdempotencyConflict):
            self.service.create_host_reboot_preview(
                planning_data(),
                self.request(reason="Different maintenance request"),
                requested_by="operator",
            )

    def test_custom_cluster_policies_are_hashed_and_exposed_by_revision(self):
        self.repository.put_policy(1, {"max_unavailable": 2}, "operator")
        self.repository.put_policy(2, {"required_cluster_health": "yellow"}, "operator")

        preview = self.service.create_host_reboot_preview(
            planning_data(),
            self.request(idempotency_key="custom-policy-request"),
            requested_by="operator",
        )

        self.assertEqual(preview["view"]["header"]["policy"]["revision"], 1)
        stored = self.repository.get_plan(preview["plan_id"])
        compiled = CompiledPlan.model_validate(stored.plan)
        self.assertEqual(
            [(item.cluster_id, item.revision) for item in compiled.observation.policies],
            [(1, 1), (2, 1)],
        )
        self.assertTrue(verify_plan_hash(compiled))

    def test_api_view_excludes_full_observations_and_redacts_sensitive_keys(self):
        preview = self.service.create_host_reboot_preview(
            planning_data(),
            self.request(reason="Routine maintenance"),
            requested_by="operator",
        )
        serialized = json.dumps(preview).lower()

        self.assertNotIn("observation", preview["view"])
        self.assertNotIn("boot_id_hash", serialized)
        self.assertNotIn("private_key", serialized)
        self.assertNotIn("password", serialized)
        self.assertNotIn("token", serialized)

    def test_host_maintenance_preview_persists_immutable_post_return_expectations(self):
        preview = self.service.create_generic_preview(
            planning_data(post_return=post_return_expectations()),
            HostMaintenancePreviewRequest(
                node_id=1,
                reason="Host patching",
                idempotency_key="host-post-return-evidence",
            ),
            requested_by="operator",
        )

        stored = self.repository.get_plan(preview["plan_id"])
        expectations = PostReturnExpectations.model_validate(
            stored.target_manifest["post_return_expectations"],
        )
        self.assertEqual(
            [(item.cluster_id, item.required_health) for item in expectations.clusters],
            [(1, "green"), (2, "green")],
        )
        self.assertEqual(expectations.clusters[0].nodes[0].persistent_node_id, "persistent-node-1")
        self.assertEqual(expectations.clusters[0].nodes[0].version, "8.19.0")
        self.assertEqual(expectations.endpoints[0].endpoint_ref, "kibana-status")
        self.assertEqual(
            [(item.cluster_id, item.role, item.minimum_available) for item in expectations.service_budgets],
            [(1, "master", 2), (2, "master", 2)],
        )


if __name__ == "__main__":
    unittest.main()
