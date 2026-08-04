import unittest
from unittest.mock import patch

from app.modules.secrets import SecretsCatalogService, redact


class SecretsModuleTests(unittest.TestCase):
    def test_redaction_is_recursive(self):
        value = redact({"token": "hidden", "nested": {"password": "hidden", "label": "ok"}})
        self.assertEqual(value["token"], "[REDACTED]")
        self.assertEqual(value["nested"]["password"], "[REDACTED]")
        self.assertEqual(value["nested"]["label"], "ok")

    def test_catalog_uses_public_cluster_and_host_projections(self):
        cluster = {
            "slug": "lab",
            "assignments": [
                {"id": 1, "role": "master", "node_id": 3, "node_name": "master-a"},
                {"id": 2, "role": "fleet-server", "node_id": 4, "node_name": "fleet-a"},
                {"id": 3, "role": "elastic-agent", "node_id": 5, "node_name": "agent-a"},
            ],
        }
        service = SecretsCatalogService(
            cluster_provider=lambda cluster_id: cluster if cluster_id == 7 else None,
            encrypted_credentials_provider=lambda cluster_id: "encrypted",
            host_provider=lambda node_id: {"id": node_id, "name": f"node-{node_id}"},
        )
        with patch("app.modules.secrets.service.open_config", return_value={"elastic_password": "present"}):
            resolved, credentials, items = service.catalog(7)

        item_ids = {item["id"] for item in items}
        paths = {item["id"]: item.get("path") for item in items}
        self.assertIs(resolved, cluster)
        self.assertEqual(credentials, {"elastic_password": "present"})
        self.assertIn("cluster.elastic_password", item_ids)
        self.assertIn("cluster.ca_certificate", item_ids)
        self.assertEqual(paths["assignment.2.fleet_service_token"], "/etc/elastic-control/clusters/lab/workloads/ecp-lab-fleet-server-4/config/fleet-service-token")
        self.assertEqual(paths["assignment.3.enrollment_token"], "/etc/elastic-control/clusters/lab/workloads/ecp-lab-elastic-agent-5/config/agent-enrollment-token")
