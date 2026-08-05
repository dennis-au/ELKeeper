from __future__ import annotations

from contextlib import contextmanager
import sqlite3
import tempfile
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.modules.certificates import CertificateLifecycleService, install_certificate_schema
from app.modules.certificates.http import build_router


class CertificateHttpTests(unittest.TestCase):
    def setUp(self):
        self.database = tempfile.NamedTemporaryFile(suffix=".db")
        with self.db() as connection:
            connection.execute(
                "CREATE TABLE clusters (id INTEGER PRIMARY KEY, desired_version TEXT NOT NULL)"
            )
            connection.execute("INSERT INTO clusters(id,desired_version) VALUES(4,'8.19.0')")
            install_certificate_schema(connection)
        self.cluster = {
            "id": 4,
            "slug": "lab-a",
            "desired_version": "8.19.0",
            "assignments": [{"id": 9, "node_id": 2, "node_name": "node-a", "role": "master"}],
        }
        service = CertificateLifecycleService(
            db_factory=self.db,
            cluster_provider=lambda cluster_id: self.cluster if cluster_id == 4 else None,
            rolling_restart_capability=lambda: False,
        )
        app = FastAPI()
        app.include_router(build_router(service=service, user_dependency=lambda: "operator"))
        self.client = TestClient(app)

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

    def test_inventory_policy_and_preview_routes_expose_only_public_lifecycle_state(self):
        inventory = self.client.get("/api/clusters/4/certificates")
        self.assertEqual(inventory.status_code, 200)
        body = inventory.json()
        self.assertEqual(body["compatibility"]["format"], "PEM")
        self.assertTrue(all("private_key" not in str(item) for item in body["items"]))

        policy = self.client.get("/api/clusters/4/certificate-policy")
        self.assertEqual(policy.status_code, 200)
        updated = self.client.put(
            "/api/clusters/4/certificate-policy",
            json={**policy.json(), "renew_before_days": 25, "expected_revision": policy.json()["revision"]},
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["renew_before_days"], 25)

        preview = self.client.post(
            f"/api/certificates/{body['items'][0]['id']}/renewal-preview"
        )
        self.assertEqual(preview.status_code, 200)
        self.assertFalse(preview.json()["execution_enabled"])
        self.assertIn("rolling_restart_capability_disabled", preview.json()["blockers"])

    def test_mutating_execute_endpoint_returns_the_capability_gate(self):
        inventory = self.client.get("/api/clusters/4/certificates").json()
        preview = self.client.post(
            f"/api/certificates/{inventory['items'][0]['id']}/renewal-preview"
        ).json()

        execution = self.client.post(
            f"/api/certificate-operations/{preview['operation_id']}/execute",
            json={"preview_hash": preview["preview_hash"]},
        )

        self.assertEqual(execution.status_code, 409)
        self.assertIn("rolling_restart_capability_disabled", execution.json()["detail"])

    def test_refresh_route_awaits_the_async_lifecycle_service_and_returns_a_redacted_run_summary(self):
        response = self.client.post("/api/clusters/4/certificates/refresh")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["mode"], "metadata_only")
        self.assertEqual(response.json()["summary"], {"collected": 0, "failed": 0})
        self.assertIsNone(response.json()["run_id"])

    def test_external_consumer_declaration_has_no_credential_surface_and_blocks_ca_retirement(self):
        inventory = self.client.get("/api/clusters/4/certificates").json()
        domain = next(item for item in inventory["trust_domains"] if item["kind"] == "elasticsearch_http")
        declared = self.client.post(
            "/api/clusters/4/certificate-trust-consumers",
            json={
                "trust_domain_id": domain["id"],
                "consumer_kind": "external_application",
                "description": "Payments API",
                "verification_method": "external_attestation",
            },
        )
        self.assertEqual(declared.status_code, 200)
        self.assertEqual(declared.json()["trust_state"], "unverified")

        preview = self.client.post("/api/clusters/4/ca-rotation-preview")
        self.assertIn("external_trust_consumer_unverified", preview.json()["blockers"])
        rejected = self.client.post(
            "/api/clusters/4/certificate-trust-consumers",
            json={
                "trust_domain_id": domain["id"],
                "consumer_kind": "external_application",
                "description": "Unsafe",
                "verification_method": "external_attestation",
                "password": "nope",
            },
        )
        self.assertEqual(rejected.status_code, 422)


if __name__ == "__main__":
    unittest.main()
