from __future__ import annotations

import unittest

from app.maintenance_provider import (
    OwnershipState,
    ProviderCapability,
    ProviderProfile,
    ProviderType,
    MaintenanceBackend,
    capability_matrix,
    provider_profile_from_record,
    require_capability,
)


class MaintenanceProviderTests(unittest.TestCase):
    def test_unverified_provider_is_read_only_except_observation(self):
        profile = ProviderProfile(
            provider_type=ProviderType.ADOPTED_PODMAN,
            ownership_state=OwnershipState.UNVERIFIED,
        )

        self.assertTrue(profile.capabilities.observation)
        for capability in ProviderCapability:
            if capability is ProviderCapability.OBSERVATION:
                continue
            self.assertFalse(getattr(profile.capabilities, capability.value))

    def test_provider_overrides_cannot_exceed_provider_boundary(self):
        profile = ProviderProfile(
            provider_type=ProviderType.ECK_ENDPOINT,
            ownership_state=OwnershipState.VERIFIED,
            maintenance_backend=MaintenanceBackend.NONE,
            capability_overrides={"host_mutation": True, "cluster_settings": True},
        )

        self.assertTrue(profile.capabilities.observation)
        self.assertFalse(profile.capabilities.host_mutation)
        self.assertFalse(profile.capabilities.cluster_settings)

    def test_native_existing_cluster_keeps_full_verified_capabilities(self):
        profile = ProviderProfile()

        self.assertEqual(profile.provider_type, ProviderType.NATIVE_PODMAN)
        self.assertEqual(profile.ownership_state, OwnershipState.VERIFIED)
        self.assertEqual(profile.capabilities.model_dump(), capability_matrix(ProviderType.NATIVE_PODMAN))

    def test_require_capability_fails_closed_with_stable_error(self):
        profile = ProviderProfile(
            provider_type=ProviderType.EXTERNAL_API,
            ownership_state=OwnershipState.VERIFIED,
        )

        with self.assertRaisesRegex(PermissionError, "workload_mutation"):
            require_capability(profile, ProviderCapability.WORKLOAD_MUTATION)

    def test_stored_profile_parses_json_and_preserves_revision(self):
        profile = provider_profile_from_record({
            "provider_type": "adopted_podman",
            "ownership_state": "verified",
            "maintenance_backend": "documented_rolling",
            "provider_capabilities_json": '{"host_mutation": false}',
            "provider_connection_json": '{"endpoint_ref": "cluster-endpoint-7"}',
            "provider_revision": 3,
        })

        self.assertEqual(profile.revision, 3)
        self.assertFalse(profile.capabilities.host_mutation)
        self.assertEqual(profile.connection_references["endpoint_ref"], "cluster-endpoint-7")


if __name__ == "__main__":
    unittest.main()
