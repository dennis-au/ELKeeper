from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from unittest.mock import AsyncMock

import asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.modules.hosts import HostLifecycleOperations, HostRemoteInspectionService, HostRepository, HostSpec, host_network_interfaces, parse_network_interfaces, ssh_error_summary
from app.modules.platform.db import connect
from app.modules.workloads import Targets


class HostsModuleTests(unittest.TestCase):
    def test_lifecycle_operations_owns_playbook_launch_composition(self):
        launches = []
        commands = []

        @contextmanager
        def database():
            yield "connection"

        def launch(kind, target, factory):
            launches.append((kind, target, factory("/tmp/inventory", None)))
            return len(launches)

        def playbook_command(*args):
            commands.append(args)
            return {"command": args}

        class Workloads:
            def __init__(self, db_factory):
                self.db_factory = db_factory

            def has_assignments_for_node(self, node_id):
                return node_id == 9

        operations = HostLifecycleOperations(
            db_factory=database,
            require_no_maintenance_conflict=lambda connection, *, node_id: self.assertEqual(
                (connection, node_id), ("connection", 7)
            ),
            workload_repository_type=Workloads,
            playbooks=Path("/playbooks"),
            active_key_path=lambda: "/run/controller-key",
            launch=launch,
            playbook_command=playbook_command,
        )

        operations.require_no_conflict(7)
        self.assertTrue(operations.has_assignments(9))
        self.assertFalse(operations.has_assignments(8))
        self.assertEqual(operations.launch_action({"name": "node-a"}, "reboot"), 1)
        self.assertEqual(operations.launch_initialize("node-b"), 2)
        self.assertEqual(
            commands,
            [
                ("/tmp/inventory", Path("/playbooks/host-reboot.yml"), "node-a", "/run/controller-key"),
                ("/tmp/inventory", Path("/playbooks/host-init.yml"), "node-b", "/run/controller-key"),
            ],
        )
        self.assertEqual([item[:2] for item in launches], [("host-reboot", "node-a"), ("host-init", "node-b")])

    def test_lifecycle_operations_builds_stable_lifecycle_and_batch_routes(self):
        conflicts = []
        launches = []
        testcase = self

        @contextmanager
        def database():
            yield "connection"

        def launch(kind, target, _factory):
            launches.append((kind, target))
            return len(launches) + 40

        class Workloads:
            def __init__(self, _db_factory):
                pass

            def has_assignments_for_node(self, node_id):
                return node_id == 2

        class Hosts:
            @classmethod
            def from_connection(cls, connection):
                testcase.assertEqual(connection, "connection")
                return cls()

            def enabled_names_in_connection(self, connection, node_ids):
                testcase.assertEqual(connection, "connection")
                testcase.assertEqual(node_ids, [3, 1])
                return ["node-c", "node-a"]

        operations = HostLifecycleOperations(
            db_factory=database,
            require_no_maintenance_conflict=lambda connection, *, node_id: conflicts.append((connection, node_id)),
            workload_repository_type=Workloads,
            playbooks=Path("/playbooks"),
            active_key_path=lambda: "/run/controller-key",
            launch=launch,
            playbook_command=lambda *args: args,
            host_repository_type=Hosts,
        )
        app = FastAPI()
        app.include_router(
            operations.lifecycle_router(
                enabled_host_provider=lambda node_id: {"id": node_id, "name": f"node-{node_id}"},
                user_dependency=lambda: "operator",
            )
        )
        app.include_router(operations.batch_router(batch_model=Targets, user_dependency=lambda: "operator"))
        client = TestClient(app)

        initialized = client.post("/api/nodes/1/initialize")
        self.assertEqual(initialized.status_code, 200)
        self.assertEqual(initialized.json(), {"run_id": 41})
        blocked = client.post("/api/nodes/2/deinitialize")
        self.assertEqual(blocked.status_code, 409)
        batch = client.post("/api/hosts/initialize", json={"node_ids": [3, 1]})
        self.assertEqual(batch.status_code, 200)
        self.assertEqual(batch.json(), {"run_ids": [42, 43]})
        self.assertEqual(conflicts, [("connection", 1), ("connection", 2), ("connection", 3), ("connection", 1)])
        self.assertEqual(
            launches,
            [("host-initialize", "node-1"), ("host-init", "node-c"), ("host-init", "node-a")],
        )

    def test_ssh_error_summary_redacts_transport_noise_into_stable_diagnoses(self):
        self.assertEqual(
            ssh_error_summary("Warning: added host\nroot@192.0.2.10: Permission denied (publickey)."),
            "Controller SSH key authentication failed",
        )
        self.assertEqual(ssh_error_summary("No route to host"), "SSH host is unreachable")
        self.assertEqual(ssh_error_summary("unexpected low-level error"), "SSH connection failed")

    def test_remote_inspection_service_builds_pinned_ssh_args_and_parses_identity(self):
        command = AsyncMock(return_value=b"ECP_OS=Test Linux\nECP_PODMAN=podman version 5.8.5\n")
        service = HostRemoteInspectionService(
            active_key_path=lambda: "/run/key",
            known_hosts_path=lambda node_ids: "/run/known_hosts",
            host_key_args=lambda node, known_hosts: ["-o", f"UserKnownHostsFile={known_hosts}"],
            remote_command=command,
            parse_counters=lambda value: {"raw": value.decode()},
        )
        node = {"id": 4, "address": "192.0.2.10", "ssh_port": 22, "ssh_user": "root"}
        self.assertIn("/run/key", service.ssh_args(node))
        self.assertEqual(asyncio.run(service.identity(node)), ("Test Linux", "5.8.5"))

    def test_remote_inspection_service_keeps_counter_parser_injected(self):
        command = AsyncMock(return_value=b"counter-output")
        service = HostRemoteInspectionService(
            active_key_path=lambda: "/run/key",
            known_hosts_path=lambda node_ids: "/run/known_hosts",
            host_key_args=lambda node, known_hosts: [],
            remote_command=command,
            parse_counters=lambda value: {"length": len(value)},
        )
        self.assertEqual(asyncio.run(service.resource_counters({"id": 4})), {"length": 14})
    def test_network_parser_is_deterministic_and_ignores_malformed_entries(self):
        parsed = parse_network_interfaces([
            {"ifname": "ens18", "addr_info": [
                {"local": "192.0.2.12"}, {"local": "192.0.2.11"}, {"local": "192.0.2.12"},
            ]},
            {"ifname": "lo", "addr_info": [{"local": "127.0.0.1"}]},
            {"ifname": "", "addr_info": [{"local": "ignored"}]},
            "malformed",
        ])
        self.assertEqual(parsed, {"ens18": ["192.0.2.11", "192.0.2.12"], "lo": ["127.0.0.1"]})
        with self.assertRaises(ValueError):
            parse_network_interfaces({"ens18": []})

    def test_network_inventory_uses_injected_remote_command_and_wraps_bad_json(self):
        command = AsyncMock(return_value=b'[{"ifname":"ens18","addr_info":[{"local":"192.0.2.10"}]}]')
        result = asyncio.run(host_network_interfaces({"id": 4}, command))
        self.assertEqual(result, {"ens18": ["192.0.2.10"]})
        command.assert_awaited_once_with({"id": 4}, "ip", "-j", "address", "show")

        broken = AsyncMock(return_value=b"not-json")
        with self.assertRaisesRegex(RuntimeError, "invalid JSON"):
            asyncio.run(host_network_interfaces({"id": 4}, broken))

    def test_host_contract_accepts_ipv4_and_rejects_dns(self):
        host = HostSpec(name="node-1", address="192.0.2.10")
        self.assertEqual(host.address, "192.0.2.10")
        with self.assertRaises(ValueError):
            HostSpec(name="node-1", address="host.example")

    def test_repository_owns_node_reads_and_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "hosts.db"
            with connect(database) as connection:
                connection.executescript(
                    """
                    CREATE TABLE nodes (
                      id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, address TEXT NOT NULL,
                      ssh_port INTEGER NOT NULL, ssh_user TEXT NOT NULL, enabled INTEGER NOT NULL,
                      zone_id TEXT, legacy_known_hosts_disabled INTEGER NOT NULL DEFAULT 0
                    );
                    """
                )
            repository = HostRepository(lambda: connect(database))
            node_id = repository.create(HostSpec(name="node-1", address="192.0.2.10").model_dump())
            self.assertEqual(repository.get(node_id)["name"], "node-1")
            self.assertTrue(repository.list()[0]["enabled"])

    def test_enabled_projection_excludes_disabled_hosts(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "hosts.db"
            with connect(database) as connection:
                connection.executescript(
                    """
                    CREATE TABLE nodes (
                      id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, address TEXT NOT NULL,
                      ssh_port INTEGER NOT NULL, ssh_user TEXT NOT NULL, enabled INTEGER NOT NULL,
                      zone_id TEXT, legacy_known_hosts_disabled INTEGER NOT NULL DEFAULT 0
                    );
                    INSERT INTO nodes(name,address,ssh_port,ssh_user,enabled)
                    VALUES ('disabled','192.0.2.11',22,'root',0);
                    """
                )
            repository = HostRepository(lambda: connect(database))
            self.assertIsNone(repository.get_enabled(1))
