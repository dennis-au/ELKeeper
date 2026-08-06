from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone

from app.maintenance_models import SourceStatus
from app.maintenance_observation import (
    collect_generic_preview_data,
    collect_host_reboot_planning_data,
)
from app.modules.maintenance.store import install_maintenance_schema


NOW = datetime(2026, 8, 3, 4, 0, tzinfo=timezone.utc)


class TelemetryStub:
    def __init__(self):
        self.host_states = {}
        self.cluster_states = {}


class MaintenanceObservationTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript("""
        CREATE TABLE nodes (
          id INTEGER PRIMARY KEY,
          name TEXT NOT NULL,
          enabled INTEGER NOT NULL
        );
        CREATE TABLE clusters (
          id INTEGER PRIMARY KEY,
          name TEXT NOT NULL
        );
        CREATE TABLE memberships (
          cluster_id INTEGER NOT NULL,
          node_id INTEGER NOT NULL,
          network_mode TEXT NOT NULL,
          data_interface TEXT,
          data_address TEXT,
          user_interface TEXT,
          user_address TEXT
        );
        CREATE TABLE cluster_assignments (
          id INTEGER PRIMARY KEY,
          cluster_id INTEGER NOT NULL,
          node_id INTEGER NOT NULL,
          role TEXT NOT NULL,
          state TEXT NOT NULL,
          revision INTEGER NOT NULL,
          operation_run_id INTEGER
        );
        CREATE TABLE runs (
          id INTEGER PRIMARY KEY,
          target TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL,
          context_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE workload_change_batches (
          run_id INTEGER PRIMARY KEY,
          cluster_id INTEGER NOT NULL
        );
        CREATE TABLE workload_observations (
          assignment_id INTEGER PRIMARY KEY,
          running INTEGER,
          observed_at TEXT,
          error TEXT
        );
        CREATE TABLE host_runtime_observations (
          node_id INTEGER PRIMARY KEY,
          initialized INTEGER NOT NULL,
          reachable INTEGER NOT NULL,
          observed_at TEXT,
          last_error TEXT NOT NULL
        );
        """)
        install_maintenance_schema(self.connection)
        self.connection.execute("INSERT INTO nodes VALUES(1,'node-a',1)")
        self.telemetry = TelemetryStub()
        self.telemetry.host_states[1] = {
            "initialized": True,
            "reachable": True,
            "observed_at": NOW,
            "last_error": "",
        }

    def tearDown(self):
        self.connection.close()

    def add_cluster(self, *, role="master", workload_observed_at=NOW, cluster_observed_at=NOW):
        self.connection.execute("INSERT INTO clusters(id,name) VALUES(1,'cluster-a')")
        self.connection.execute(
            "INSERT INTO memberships VALUES(1,1,'shared','ens18','192.0.2.10','ens18','192.0.2.10')"
        )
        self.connection.execute(
            "INSERT INTO cluster_assignments(id,cluster_id,node_id,role,state,revision) "
            "VALUES(1,1,1,?,'active',1)",
            (role,),
        )
        stored_workload_time = (
            workload_observed_at.isoformat() if isinstance(workload_observed_at, datetime)
            else workload_observed_at
        )
        self.connection.execute(
            "INSERT INTO workload_observations(assignment_id,running,observed_at,error) VALUES(1,1,?,'')",
            (stored_workload_time,),
        )
        self.telemetry.cluster_states[1] = {
            "cluster_name": "cluster-a",
            "cluster_uuid": "observed-cluster-uuid",
            "status": "green",
            "observed_at": cluster_observed_at,
            "last_error": "",
            "initializing_shards": 0,
            "relocating_shards": 0,
            "no_last_shard_copy": True,
            "primary_promotion_safe": True,
            "shard_safety_observed": False,
            "disk_total_bytes": 100,
            "disk_available_bytes": 60,
            "stale_shutdown_record": False,
        }

    def collect(self, *, include_post_return_expectations=False):
        return collect_host_reboot_planning_data(
            self.connection,
            self.telemetry,
            node_id=1,
            capability_revision="test-capabilities",
            include_post_return_expectations=include_post_return_expectations,
            clock=lambda: NOW,
        )

    @staticmethod
    def source(data, name):
        return next(item for item in data.sources if item.source == name)

    def test_runtime_datetime_values_are_accepted_without_false_errors(self):
        data = self.collect()

        self.assertEqual(self.source(data, "runtime").status, SourceStatus.OK)
        self.assertEqual(data.hosts[0].observed_at, NOW)

    def test_malformed_host_timestamp_fails_closed(self):
        self.telemetry.host_states[1]["observed_at"] = "not-a-timestamp"

        data = self.collect()

        self.assertEqual(self.source(data, "runtime").status, SourceStatus.ERROR)
        self.assertEqual(self.source(data, "runtime").error_category, "runtime-observation-invalid")

    def test_malformed_workload_and_elasticsearch_timestamps_fail_independently(self):
        self.telemetry.host_states[1]["network_interfaces"] = {
            "ens18": ["192.0.2.10/24"],
        }
        self.add_cluster(
            workload_observed_at="not-a-workload-time",
            cluster_observed_at="not-an-elasticsearch-time",
        )

        data = self.collect()

        self.assertEqual(self.source(data, "runtime").status, SourceStatus.ERROR)
        self.assertEqual(self.source(data, "runtime").error_category, "runtime-observation-invalid")
        self.assertEqual(self.source(data, "elasticsearch").status, SourceStatus.ERROR)
        self.assertEqual(
            self.source(data, "elasticsearch").error_category,
            "elasticsearch-observation-invalid",
        )

    def test_membership_requires_observed_interface_and_address(self):
        self.add_cluster()

        missing_inventory = self.collect()
        self.assertFalse(missing_inventory.hosts[0].membership_ready)
        self.assertEqual(self.source(missing_inventory, "membership").status, SourceStatus.ERROR)

        self.telemetry.host_states[1]["network_interfaces"] = {
            "ens18": ["192.0.2.10/24"],
        }
        observed_inventory = self.collect()
        self.assertTrue(observed_inventory.hosts[0].membership_ready)
        self.assertEqual(self.source(observed_inventory, "membership").status, SourceStatus.OK)

    def test_hot_workload_is_treated_as_hot_and_content_data(self):
        self.telemetry.host_states[1]["network_interfaces"] = {
            "ens18": ["192.0.2.10"],
        }
        self.add_cluster(role="hot")

        data = self.collect()

        self.assertEqual(data.workloads[0].data_tiers, ("content", "hot"))
        self.assertEqual(self.source(data, "shard-safety").status, SourceStatus.MISSING)

    def test_cluster_identity_fails_closed_without_configured_uuid(self):
        self.telemetry.host_states[1]["network_interfaces"] = {
            "ens18": ["192.0.2.10"],
        }
        self.add_cluster()

        data = self.collect()

        self.assertIsNone(data.clusters[0].configured_uuid)
        self.assertFalse(data.clusters[0].identity_matches)
        self.assertEqual(self.source(data, "cluster-identity").status, SourceStatus.MISSING)

    def test_generic_host_preview_retains_immutable_runtime_return_evidence(self):
        self.telemetry.host_states[1]["network_interfaces"] = {
            "ens18": ["192.0.2.10"],
        }
        self.add_cluster()
        self.connection.execute("ALTER TABLE clusters ADD COLUMN slug TEXT")
        self.connection.execute("UPDATE clusters SET slug='cluster-a' WHERE id=1")
        self.connection.execute("ALTER TABLE workload_observations ADD COLUMN version TEXT")
        self.connection.execute("UPDATE workload_observations SET version='8.19.0' WHERE assignment_id=1")
        self.telemetry.cluster_states[1]["node_breakdown"] = [{
            "id": "persistent-node-1",
            "name": "ecp-cluster-a-master-1",
        }]

        data = collect_generic_preview_data(
            self.connection,
            self.telemetry,
            node_ids=(1,),
            capability_revision="test-capabilities",
            include_post_return_expectations=True,
            clock=lambda: NOW,
        )

        expectations = data.post_return_expectations
        self.assertIsNotNone(expectations)
        assert expectations is not None
        self.assertEqual(expectations.clusters[0].nodes[0].persistent_node_id, "persistent-node-1")
        self.assertEqual(expectations.clusters[0].nodes[0].version, "8.19.0")
        self.assertEqual(expectations.clusters[0].nodes[0].cluster_uuid, "observed-cluster-uuid")
        self.assertEqual(expectations.endpoints[0].endpoint_ref, "assignment-1")


if __name__ == "__main__":
    unittest.main()
