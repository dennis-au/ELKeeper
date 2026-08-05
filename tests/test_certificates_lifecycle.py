from __future__ import annotations

from contextlib import contextmanager
import sqlite3
import tempfile
import unittest

from app.modules.certificates import (
    CertificateLifecycleService,
    CertificateRepository,
    CertificateRevisionConflict,
    install_certificate_schema,
)


class CertificateLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.database = tempfile.NamedTemporaryFile(suffix=".db")
        with self.db() as connection:
            connection.execute(
                "CREATE TABLE clusters (id INTEGER PRIMARY KEY, desired_version TEXT NOT NULL)"
            )
            connection.execute("INSERT INTO clusters(id,desired_version) VALUES(7,'8.19.0')")
            install_certificate_schema(connection)
        self.cluster = {
            "id": 7,
            "slug": "lab-a",
            "desired_version": "8.19.0",
            "assignments": [
                {"id": 11, "node_id": 3, "node_name": "node-a", "role": "master"},
                {"id": 12, "node_id": 4, "node_name": "node-b", "role": "kibana"},
            ],
        }
        self.audit_events: list[tuple[str, str, int, str, dict]] = []
        self.service = CertificateLifecycleService(
            db_factory=self.db,
            cluster_provider=lambda cluster_id: self.cluster if cluster_id == 7 else None,
            audit_event=lambda username, action, cluster_id, item_id, detail: self.audit_events.append(
                (username, action, cluster_id, item_id, detail)
            ),
            rolling_restart_capability=lambda: False,
        )

    def tearDown(self):
        self.database.close()

    @contextmanager
    def db(self):
        connection = sqlite3.connect(self.database.name)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def test_legacy_adoption_creates_separate_transport_and_http_domains_without_remote_mutation(self):
        inventory = self.service.list_assets(7)

        self.assertEqual(len(inventory["trust_domains"]), 4)
        domain_kinds = {item["kind"] for item in inventory["trust_domains"]}
        self.assertEqual(
            domain_kinds,
            {"elasticsearch_transport", "elasticsearch_http", "kibana_http", "fleet_http"},
        )
        self.assertTrue(all(item["legacy_shared"] for item in inventory["trust_domains"]))
        self.assertTrue(all(item["management_state"] == "observed" for item in inventory["items"]))
        self.assertTrue(all(item["health"] == "unobserved" for item in inventory["items"]))
        self.assertTrue(any(item["purpose"] == "elasticsearch_transport" for item in inventory["items"]))
        self.assertTrue(any(item["purpose"] == "kibana_server" for item in inventory["items"]))

    def test_policy_defaults_are_approval_required_and_reject_stale_updates(self):
        policy = self.service.policy(7)
        self.assertEqual(policy["renewal_mode"], "approval_required")
        self.assertEqual(policy["renew_before_days"], 30)
        self.assertEqual(policy["critical_before_days"], 14)
        self.assertEqual(policy["default_validity_days"], 90)

        updated = self.service.update_policy(
            7,
            {**policy, "renew_before_days": 21, "expected_revision": policy["revision"]},
            username="operator",
        )
        self.assertEqual(updated["renew_before_days"], 21)
        self.assertEqual(updated["revision"], policy["revision"] + 1)
        self.assertEqual(updated["issuer_validity_days"], 365)
        self.assertEqual(self.audit_events[-1][1], "certificate-policy-updated")

        with self.assertRaises(CertificateRevisionConflict):
            self.service.update_policy(
                7,
                {**updated, "renew_before_days": 20, "expected_revision": policy["revision"]},
                username="operator",
            )

    def test_renewal_preview_is_non_mutating_and_gated_by_rolling_restart_capability(self):
        asset = self.service.list_assets(7)["items"][0]

        preview = self.service.renewal_preview(asset["id"], username="operator")

        self.assertEqual(preview["state"], "blocked")
        self.assertIn("rolling_restart_capability_disabled", preview["blockers"])
        self.assertFalse(preview["execution_enabled"])
        self.assertEqual(preview["operation_type"], "leaf_renewal")
        self.assertIsNone(preview["run_id"])

    def test_compatibility_is_pem_only_for_elastic_8_and_9_and_blocks_unsupported_versions(self):
        current = self.service.compatibility(7)
        self.assertTrue(current["supported"])
        self.assertEqual(current["format"], "PEM")
        self.assertFalse(current["reload_enabled"])

        self.cluster["desired_version"] = "9.1.0"
        self.assertTrue(self.service.compatibility(7)["supported"])

        self.cluster["desired_version"] = "7.17.0"
        unsupported = self.service.compatibility(7)
        self.assertFalse(unsupported["supported"])
        self.assertEqual(unsupported["reason_code"], "unsupported_elastic_version")

    def test_schema_never_has_private_key_material_columns(self):
        with self.db() as connection:
            repository = CertificateRepository(connection)
            tables = repository.schema_tables()
            columns = {
                table: {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
                for table in tables
            }

        prohibited = {"private_key", "private_key_pem", "pem_private_key", "password"}
        self.assertFalse(any(prohibited.intersection(values) for values in columns.values()))

    def test_external_trust_consumer_blocks_ca_rotation_and_rejects_credentials(self):
        inventory = self.service.list_assets(7)
        http_domain = next(item for item in inventory["trust_domains"] if item["kind"] == "elasticsearch_http")
        consumer = self.service.declare_external_consumer(
            7,
            {
                "trust_domain_id": http_domain["id"],
                "consumer_kind": "external_application",
                "description": "Payments API",
                "verification_method": "external_attestation",
            },
            username="operator",
        )
        self.assertEqual(consumer["consumer_type"], "external")
        self.assertEqual(consumer["trust_state"], "unverified")

        preview = self.service.ca_rotation_preview(7, username="operator")
        self.assertIn("external_trust_consumer_unverified", preview["blockers"])

        with self.assertRaisesRegex(Exception, "Unexpected trust consumer fields"):
            self.service.declare_external_consumer(
                7,
                {
                    "trust_domain_id": http_domain["id"],
                    "consumer_kind": "external_application",
                    "description": "Unsafe client",
                    "verification_method": "external_attestation",
                    "password": "not-allowed",
                },
                username="operator",
            )


if __name__ == "__main__":
    unittest.main()
