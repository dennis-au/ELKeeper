import unittest

from app.modules.certificates import CertificateInventoryService


class CertificateInventoryTests(unittest.TestCase):
    def setUp(self):
        self.service = CertificateInventoryService(
            lambda node_id: {"id": node_id, "name": f"node-{node_id}"},
        )
        self.cluster = {
            "slug": "lab",
            "assignments": [
                {"id": 1, "role": "master", "node_id": 3, "node_name": "master-a"},
            ],
        }

    def test_cluster_inventory_contains_only_allowlisted_paths(self):
        items = self.service.cluster_items(self.cluster)
        self.assertEqual([item["id"] for item in items], ["cluster.ca_certificate", "cluster.ca_private_key"])
        self.assertEqual(items[0]["path"], "/etc/elastic-control/clusters/lab/ca/ca.crt")
        self.assertTrue(items[0]["certificate"])
        self.assertNotIn("value", items[0])

    def test_workload_inventory_includes_certificate_key_and_fleet_token_paths(self):
        assignment = {"id": 7, "role": "fleet-server", "node_id": 4, "node_name": "fleet-a"}
        items = self.service.workload_items(self.cluster, assignment)
        paths = {item["id"]: item["path"] for item in items}
        self.assertEqual(paths["assignment.7.certificate"], "/etc/elastic-control/clusters/lab/workloads/ecp-lab-fleet-server-4/certs/node.crt")
        self.assertEqual(paths["assignment.7.private_key"], "/etc/elastic-control/clusters/lab/workloads/ecp-lab-fleet-server-4/certs/node.key")
        self.assertEqual(paths["assignment.7.fleet_service_token"], "/etc/elastic-control/clusters/lab/workloads/ecp-lab-fleet-server-4/config/fleet-service-token")

    def test_missing_host_is_rejected(self):
        service = CertificateInventoryService(lambda _node_id: None)
        with self.assertRaises(KeyError):
            service.cluster_items(self.cluster)


if __name__ == "__main__":
    unittest.main()
