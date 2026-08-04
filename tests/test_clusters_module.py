import asyncio
import sqlite3
import unittest
from types import SimpleNamespace

from fastapi import HTTPException

from app.modules.clusters import ClusterSettingsService, MembershipOperations, RolePortProfile, default_role_ports, membership_ready, validate_membership_network, valid_ipv4


class ClusterNetworkTests(unittest.TestCase):
    def test_role_port_contract_allocates_unique_role_qualified_ports(self):
        profile = RolePortProfile.model_validate(default_role_ports())
        values = [port for association in profile.model_dump(by_alias=True).values() for port in association.values()]
        self.assertEqual(len(values), len(set(values)))

    def test_shared_and_dedicated_network_modes(self):
        self.assertTrue(valid_ipv4("192.0.2.10"))
        self.assertFalse(valid_ipv4("cluster.example"))
        shared = SimpleNamespace(network_mode="shared", data_interface="ens18", user_interface="ens18", data_address="192.0.2.10", user_address="192.0.2.10")
        validate_membership_network(shared)
        dedicated = SimpleNamespace(network_mode="dedicated", data_interface="ens19", user_interface="ens18", data_address="198.51.100.10", user_address="192.0.2.10")
        validate_membership_network(dedicated)
        self.assertFalse(membership_ready({"network_mode": "dedicated", "data_interface": "ens18", "user_interface": "ens18", "data_address": "192.0.2.10", "user_address": "192.0.2.10"}))


class ClusterSettingsServiceTests(unittest.TestCase):
    def test_settings_without_master_persist_without_remote_launch(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(
            "CREATE TABLE clusters(id INTEGER PRIMARY KEY, elasticsearch_settings_json TEXT, secrets_json TEXT)"
        )
        connection.execute("INSERT INTO clusters VALUES (1, '{}', '{}')")
        completed = []
        launched = []

        class Settings:
            def model_dump_json(self):
                return '{"cluster.routing.allocation.enable":"all"}'

            def model_dump(self):
                return {"cluster.routing.allocation.enable": "all"}

        service = ClusterSettingsService(
            db_factory=lambda: connection,
            cluster_record=lambda _connection, _cluster_id: {
                "id": 1,
                "name": "lab",
                "theme_color": "#000000",
                "desired_version": "8.19.0",
                "network_defaults": {},
                "elasticsearch_settings": {},
                "assignments": [],
                "members": [],
            },
            require_no_maintenance_conflict=lambda *_args, **_kwargs: None,
            require_cluster_capability=lambda *_args, **_kwargs: None,
            settings_capability=object(),
            open_config=lambda _value: {},
            completed_run=lambda *args: completed.append(args) or 71,
            launch_settings=lambda *args: launched.append(args) or 72,
        )

        result = asyncio.run(service.update(1, Settings(), "operator"))

        self.assertEqual(result, {"updated": True, "run_id": 71})
        self.assertEqual(launched, [])
        self.assertEqual(completed[0][:3], ("cluster-settings", "lab", "Stored settings; no master is assigned"))
        self.assertEqual(
            connection.execute("SELECT elasticsearch_settings_json FROM clusters WHERE id=1").fetchone()[0],
            '{"cluster.routing.allocation.enable":"all"}',
        )

    def test_settings_service_uses_cluster_repository_for_settings_and_credentials(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(
            "CREATE TABLE clusters(id INTEGER PRIMARY KEY, elasticsearch_settings_json TEXT, secrets_json TEXT)"
        )
        connection.execute("INSERT INTO clusters VALUES (1, '{}', '{\"elastic_password\":\"sealed\"}')")

        class Settings:
            def model_dump_json(self):
                return '{"indices.recovery.max_bytes_per_sec":"40mb"}'

            def model_dump(self):
                return {"indices.recovery.max_bytes_per_sec": "40mb"}

        launched = []
        service = ClusterSettingsService(
            db_factory=lambda: connection,
            cluster_record=lambda _connection, _cluster_id: {
                "id": 1,
                "name": "lab",
                "theme_color": "#000000",
                "desired_version": "8.19.0",
                "network_defaults": {},
                "elasticsearch_settings": {},
                "assignments": [{"role": "master", "node_id": 2}],
                "members": [{"node_id": 2}],
            },
            require_no_maintenance_conflict=lambda *_args, **_kwargs: None,
            require_cluster_capability=lambda *_args, **_kwargs: None,
            settings_capability=object(),
            open_config=lambda value: {"opened": value},
            completed_run=lambda *_args: 71,
            launch_settings=lambda *args: launched.append(args) or 72,
        )

        result = asyncio.run(service.update(1, Settings(), "operator"))

        self.assertEqual(result, {"updated": True, "run_id": 72})
        self.assertEqual(launched[0][4], {"opened": '{"elastic_password":"sealed"}'})
        self.assertEqual(
            connection.execute("SELECT elasticsearch_settings_json FROM clusters WHERE id=1").fetchone()[0],
            '{"indices.recovery.max_bytes_per_sec":"40mb"}',
        )


class MembershipOperationsTests(unittest.TestCase):
    def test_membership_operations_own_zone_readiness_and_repository_delegation(self):
        events = []

        class ClusterRepository:
            @staticmethod
            def from_connection(_connection):
                return SimpleNamespace(
                    insert_membership_in_connection=lambda connection, cluster_id, membership: events.append(("insert", connection, cluster_id, membership)),
                    update_membership_in_connection=lambda connection, cluster_id, node_id, membership: events.append(("update", connection, cluster_id, node_id, membership)) or True,
                    delete_membership_in_connection=lambda connection, cluster_id, node_id: events.append(("delete", connection, cluster_id, node_id)),
                )

        class HostRepository:
            @staticmethod
            def from_connection(_connection):
                return SimpleNamespace(get=lambda node_id: {"id": node_id, "zone_id": "zone-a"})

        class WorkloadRepository:
            @staticmethod
            def from_connection(_connection):
                return SimpleNamespace(has_assignments_for_member_in_connection=lambda _connection, cluster_id, node_id: (cluster_id, node_id) == (1, 2))

        operations = MembershipOperations(
            cluster_repository=ClusterRepository,
            host_repository=HostRepository,
            workload_repository=WorkloadRepository,
        )
        connection = object()
        membership = object()

        self.assertEqual(operations.stored_zoning('{"mode":"awareness","zones":["zone-a","zone-b"]}').zones, ["zone-a", "zone-b"])
        self.assertEqual(operations.node_record(connection, 2), {"id": 2, "zone_id": "zone-a"})
        operations.insert(connection, 1, membership)
        self.assertTrue(operations.update(connection, 1, 2, membership))
        self.assertTrue(operations.has_assignments(connection, 1, 2))
        operations.delete(connection, 1, 2)
        self.assertEqual([event[0] for event in events], ["insert", "update", "delete"])

        operations.require_cluster_host_zone(
            {"zoning": {"mode": "awareness", "zones": ["zone-a", "zone-b"]}},
            {"zone_id": "zone-a"},
        )
        operations.require_ready(
            {"network_mode": "shared", "data_interface": "ens18", "user_interface": "ens18", "data_address": "192.0.2.10", "user_address": "192.0.2.10"}
        )
        with self.assertRaises(HTTPException):
            operations.require_cluster_host_zone(
                {"zoning": {"mode": "awareness", "zones": ["zone-a", "zone-b"]}},
                {"zone_id": "missing"},
            )
        with self.assertRaises(HTTPException):
            operations.require_ready({"network_mode": "shared"})
