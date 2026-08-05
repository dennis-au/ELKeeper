import unittest

from app.modules.maintenance.evacuation_contracts import (
    EvacuationPreview,
    ProviderCapability,
    build_evacuation_preview,
    build_inventory_evacuation_preview,
)
from app.modules.maintenance.upgrade_contracts import (
    UpgradeArtifact,
    UpgradePreflight,
    build_upgrade_manifest,
    validate_upgrade_transition,
)
from app.modules.maintenance.upgrade_planning import (
    UpgradePlanPreview,
    attach_manifest,
    build_manifest_for_assignments,
    build_upgrade_plan_preview,
    manifest_from_target_manifest,
)


class UpgradeContractTests(unittest.TestCase):
    def artifact(self, version="8.19.1", digest="a"):
        return UpgradeArtifact(
            assignment_id=1,
            node_id=2,
            role="kibana",
            image="docker.elastic.co/kibana/kibana:" + version,
            version=version,
            digest="sha256:" + digest * 64,
        )

    def test_manifest_requires_unique_assignment_and_stable_digest(self):
        manifest = build_upgrade_manifest([self.artifact()])
        self.assertEqual(manifest.artifacts[0].digest, "sha256:" + "a" * 64)
        self.assertEqual(len(manifest.manifest_hash), 64)
        with self.assertRaises(ValueError):
            build_upgrade_manifest([self.artifact(), self.artifact()])

    def test_preflight_blocks_major_upgrade_without_three_masters_and_snapshot(self):
        result = validate_upgrade_transition(
            current_version="8.18.2",
            target_version="9.0.0",
            preflight=UpgradePreflight(
                cluster_healthy=True,
                master_eligible_available=2,
                snapshot_age_seconds=None,
                target_artifacts_ready=True,
            ),
        )
        self.assertIn("master_redundancy_required", result)
        self.assertIn("recent_snapshot_required", result)

    def test_downgrade_is_always_blocked(self):
        result = validate_upgrade_transition(
            current_version="8.19.1",
            target_version="8.18.2",
            preflight=UpgradePreflight(
                cluster_healthy=True,
                master_eligible_available=3,
                snapshot_age_seconds=1,
                target_artifacts_ready=True,
            ),
        )
        self.assertEqual(result, ("downgrade_not_supported",))

    def test_preflight_blocks_stale_identity_conflict_and_unverified_snapshot(self):
        result = validate_upgrade_transition(
            current_version="8.19.1",
            target_version="9.0.0",
            preflight=UpgradePreflight(
                cluster_healthy=True,
                master_eligible_available=3,
                snapshot_age_seconds=1,
                target_artifacts_ready=True,
                observations_fresh=False,
                cluster_identity_matches=False,
                no_conflicting_operation=False,
                snapshot_verified=False,
                quorum_preserved=False,
            ),
        )
        self.assertIn("stale_runtime_observation", result)
        self.assertIn("expected_cluster_identity_required", result)
        self.assertIn("conflicting_operation", result)
        self.assertIn("master_quorum_not_preserved", result)
        self.assertIn("snapshot_verification_required", result)

    def test_manifest_projection_round_trips_through_target_manifest(self):
        manifest = build_upgrade_manifest([self.artifact()])
        target = attach_manifest({"public_operation": "upgrade"}, manifest)
        loaded = manifest_from_target_manifest(target)
        self.assertEqual(loaded.manifest_hash, manifest.manifest_hash)
        self.assertEqual(target["public_operation"], "upgrade")
        target["upgrade_manifest_hash"] = "0" * 64
        with self.assertRaises(ValueError):
            manifest_from_target_manifest(target)

    def test_assignment_manifest_requires_every_immutable_target_digest(self):
        assignments = [{"id": 1, "node_id": 2, "role": "kibana"}]
        image_for_role = lambda role, version: f"docker.elastic.co/{role}:{version}"
        with self.assertRaises(ValueError):
            build_manifest_for_assignments(
                assignments,
                target_version="8.20.0",
                image_for_role=image_for_role,
                target_digests={},
            )

    def test_upgrade_preview_is_always_execution_disabled_and_hash_stable(self):
        manifest = build_upgrade_manifest([self.artifact()])
        preview = build_upgrade_plan_preview(
            cluster_id=4,
            current_version="8.19.0",
            target_version="8.19.1",
            manifest=manifest,
            preflight=UpgradePreflight(
                cluster_healthy=True,
                master_eligible_available=3,
                snapshot_age_seconds=1,
                target_artifacts_ready=True,
            ),
        )
        self.assertIsInstance(preview, UpgradePlanPreview)
        self.assertFalse(preview.execution_enabled)
        self.assertTrue(preview.ready)
        self.assertEqual(preview.manifest.manifest_hash, manifest.manifest_hash)

    def test_upgrade_preview_rejects_manifest_for_a_different_tag(self):
        with self.assertRaises(ValueError):
            build_upgrade_plan_preview(
                cluster_id=4,
                current_version="8.19.0",
                target_version="8.20.0",
                manifest=build_upgrade_manifest([self.artifact(version="8.19.1")]),
                preflight=UpgradePreflight(
                    cluster_healthy=True,
                    master_eligible_available=3,
                    snapshot_age_seconds=1,
                    target_artifacts_ready=True,
                ),
            )


class EvacuationContractTests(unittest.TestCase):
    def test_endpoint_only_provider_is_preview_only(self):
        preview = build_evacuation_preview(
            provider=ProviderCapability.ECK_ENDPOINT,
            source_node_id=3,
            replacement_node_id=4,
            available_capacity=2,
            required_capacity=1,
            max_surge=0,
        )
        self.assertIsInstance(preview, EvacuationPreview)
        self.assertFalse(preview.mutation_allowed)
        self.assertIn("provider_read_only", preview.blockers)

    def test_inventory_preview_derives_port_zone_and_capacity_blockers(self):
        inventory = {
            "cluster": {
                "id": 7,
                "provider_type": "native_podman",
                "zoning": {"mode": "forced_awareness"},
                "role_ports": {
                    "hot": {"elasticsearch_http": 9200, "elasticsearch_transport": 9300},
                    "master": {"elasticsearch_http": 9201, "elasticsearch_transport": 9301},
                },
            },
            "clusters": [{
                "id": 7,
                "provider_type": "native_podman",
                "role_ports": {
                    "hot": {"elasticsearch_http": 9200, "elasticsearch_transport": 9300},
                    "master": {"elasticsearch_http": 9201, "elasticsearch_transport": 9301},
                },
                "zoning": {"mode": "forced_awareness"},
            }],
            "source": {"id": 1, "enabled": True, "zone_id": "a"},
            "replacement": {"id": 2, "enabled": True, "zone_id": "a"},
            "source_node_id": 1,
            "replacement_node_id": 2,
            "source_membership": {
                "network_mode": "shared", "data_interface": "ens18", "data_address": "192.0.2.1",
                "user_interface": "ens18", "user_address": "192.0.2.1",
            },
            "replacement_membership": {
                "network_mode": "shared", "data_interface": "ens18", "data_address": "192.0.2.2",
                "user_interface": "ens18", "user_address": "192.0.2.2",
            },
            "source_runtime": {"initialized": True, "reachable": True, "network_interfaces": {"ens18": ["192.0.2.1"]}},
            "replacement_runtime": {"initialized": True, "reachable": True, "network_interfaces": {"ens18": ["192.0.2.2"]}},
            "source_assignments": [{
                "cluster_id": 7, "role": "hot",
                "resource": {"cpu": "1", "memory_bytes": 2147483648, "storage_managed": True},
                "observation": {"image": "image", "digest": "sha256:abc", "cached": True},
            }],
            "replacement_assignments": [{"cluster_id": 7, "role": "hot"}],
            "max_surge": 0,
        }
        preview = build_inventory_evacuation_preview(inventory)
        self.assertFalse(preview.mutation_allowed)
        self.assertEqual(preview.cluster_id, 7)
        self.assertEqual(preview.required_capacity, 1)
        self.assertIsNone(preview.available_capacity)
        self.assertIn("replacement_port_conflict", preview.blockers)
        self.assertIn("replacement_zone_not_diverse", preview.blockers)
        self.assertIn("replacement_capacity_unobserved", preview.blockers)
        self.assertEqual(preview.evidence["required_cpu_cores"], 1.0)
