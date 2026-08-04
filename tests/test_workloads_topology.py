import unittest

from app.modules.workloads import configured_access_urls, render_topology


class WorkloadTopologyTests(unittest.TestCase):
    def test_access_urls_use_user_address_only(self):
        cluster = {
            "role_ports": {"kibana": {"kibana": 5602}},
            "members": [{"node_id": 1, "user_address": "192.0.2.20", "data_address": "198.51.100.20"}],
            "assignments": [{"id": 5, "node_id": 1, "role": "kibana"}],
        }
        urls = configured_access_urls(cluster, lambda value: value.count(".") == 3)
        self.assertEqual(urls[0]["url"], "https://192.0.2.20:5602")
        self.assertNotIn("198.51.100.20", urls[0]["url"])

    def test_rendering_contains_host_and_role_boxes_and_transport_connector(self):
        cluster = {
            "name": "demo", "slug": "demo", "role_ports": {
                "master": {"elasticsearch_http": 9200, "elasticsearch_transport": 9300},
                "hot": {"elasticsearch_http": 9200, "elasticsearch_transport": 9300},
            },
            "members": [
                {"node_id": 1, "name": "one", "zone_id": None, "network_mode": "shared", "user_interface": "ens18", "user_address": "192.0.2.1", "data_interface": "ens18", "data_address": "192.0.2.1"},
                {"node_id": 2, "name": "two", "zone_id": None, "network_mode": "shared", "user_interface": "ens18", "user_address": "192.0.2.2", "data_interface": "ens18", "data_address": "192.0.2.2"},
            ],
            "assignments": [
                {"id": 1, "node_id": 1, "role": "master", "config": {"cpu": "2", "memory": "4g", "storage_path": "/srv/es1"}},
                {"id": 2, "node_id": 2, "role": "hot", "config": {"cpu": "2", "memory": "4g", "storage_path": "/srv/es2"}},
            ],
        }
        role_specs = {"master": {"label": "Master"}, "hot": {"label": "Hot data"}}
        output, _ = render_topology(cluster, role_specs, {"master": "master", "hot": "data_hot"}, lambda value: True)
        self.assertIn("HOST: one", output)
        self.assertIn("Name     : ecp-demo-master-1", output)
        self.assertIn("Elasticsearch transport", output)
