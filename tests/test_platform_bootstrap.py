import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace

from app.modules.platform.bootstrap import (
    SchemaIntrospection,
    ControllerBootstrapService,
    apply_controller_schema_upgrades,
    complete_controller_bootstrap,
    bootstrap_controller_schema,
    ensure_migration_ledger,
    ensure_runtime_directories,
)
from app.modules.platform.config import RuntimePaths
from app.modules.platform.db import connect
from app.modules.platform.runs import finish_run_in_connection, mark_recovery_required_in_connection


def _runtime_paths(base: Path) -> RuntimePaths:
    return RuntimePaths(
        base,
        base / "runs",
        base / "inventory",
        base / "variables",
        base / "runtime",
        base / "runtime" / "ssh",
    )


def _apply_fixture_upgrades(connection: sqlite3.Connection) -> None:
    """Use deterministic product callbacks for durable-schema fixtures."""

    def stored_role_ports(encoded: str, legacy_ports: dict) -> dict:
        return json.loads(encoded) if encoded != "{}" else {"elasticsearch": legacy_ports}

    def open_fixture_config(encoded: str) -> dict:
        return json.loads(encoded.removeprefix("sealed:"))

    apply_controller_schema_upgrades(
        connection,
        default_stack_version="9.1.0",
        theme_palette=("blue", "green", "orange"),
        network_defaults_json='{"mode":"shared"}',
        elasticsearch_settings_json='{"indices.recovery.max_bytes_per_sec":"40mb"}',
        stored_role_ports=stored_role_ports,
        log_monitoring_config=lambda encoded: json.loads(encoded) if encoded != "{}" else {"enabled": False},
        open_config=open_fixture_config,
        seal_config=lambda value: "sealed:" + value,
        token_factory=lambda length: "x" * length,
    )


def _create_oldest_supported_schema(connection: sqlite3.Connection) -> None:
    """Create the oldest retained controller schema with representative data."""

    connection.executescript("""
        CREATE TABLE users (username TEXT PRIMARY KEY, password_hash TEXT NOT NULL);
        CREATE TABLE controller_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE nodes (
          id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, address TEXT NOT NULL,
          ssh_port INTEGER NOT NULL, ssh_user TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE assignments (
          node_id INTEGER NOT NULL, role TEXT NOT NULL, config_json TEXT NOT NULL DEFAULT '{}',
          PRIMARY KEY(node_id, role)
        );
        CREATE TABLE clusters (
          id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, slug TEXT UNIQUE NOT NULL,
          ports_json TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE memberships (
          cluster_id INTEGER NOT NULL, node_id INTEGER NOT NULL, advertised_address TEXT,
          PRIMARY KEY(cluster_id, node_id)
        );
        CREATE TABLE cluster_assignments (
          id INTEGER PRIMARY KEY, cluster_id INTEGER NOT NULL, node_id INTEGER NOT NULL,
          role TEXT NOT NULL, config_json TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'active',
          UNIQUE(cluster_id, node_id, role)
        );
        CREATE TABLE workload_change_batches (
          run_id INTEGER PRIMARY KEY, cluster_id INTEGER NOT NULL, plan_encrypted TEXT NOT NULL,
          completed_json TEXT NOT NULL DEFAULT '[]', phase TEXT NOT NULL DEFAULT 'applying'
        );
        CREATE TABLE workload_observations (
          assignment_id INTEGER PRIMARY KEY, image TEXT NOT NULL DEFAULT '', digest TEXT NOT NULL DEFAULT '',
          version TEXT NOT NULL DEFAULT '', running INTEGER NOT NULL DEFAULT 0,
          observed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, error TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE runs (
          id INTEGER PRIMARY KEY, kind TEXT NOT NULL, target TEXT NOT NULL, status TEXT NOT NULL,
          command_json TEXT NOT NULL, log TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, finished_at TEXT
        );
        CREATE TABLE host_runtime_observations (
          node_id INTEGER PRIMARY KEY, initialized INTEGER NOT NULL DEFAULT 0,
          reachable INTEGER NOT NULL DEFAULT 0, podman_socket_active INTEGER NOT NULL DEFAULT 0,
          podman_version TEXT NOT NULL DEFAULT '', observed_at TEXT, last_error TEXT NOT NULL DEFAULT ''
        );
    """)
    connection.execute("INSERT INTO users VALUES ('operator', 'historic-password-hash')")
    connection.execute(
        "INSERT INTO nodes(id,name,address,ssh_port,ssh_user) VALUES(1,'legacy-node','198.51.100.10',22,'root')"
    )
    connection.execute(
        "INSERT INTO clusters(id,name,slug,ports_json) VALUES(1,'Legacy','legacy','{\"elasticsearch_http\":9200}')"
    )
    connection.execute("INSERT INTO memberships VALUES(1,1,'198.51.100.10')")
    connection.execute(
        "INSERT INTO cluster_assignments(id,cluster_id,node_id,role,config_json) VALUES(1,1,1,'master','sealed:{}')"
    )
    connection.execute(
        "INSERT INTO workload_observations(assignment_id,image,version,running) VALUES(1,'elastic:old','8.0.0',1)"
    )


def _complete_fixture_bootstrap(connection: sqlite3.Connection, *, protected_run_ids: frozenset[int] = frozenset()) -> frozenset[int]:
    return complete_controller_bootstrap(
        connection,
        maintenance_migrations=(
            lambda database: database.execute(
                "CREATE TABLE IF NOT EXISTS maintenance_plans(id INTEGER PRIMARY KEY, run_id INTEGER)"
            ),
        ),
        prepare_maintenance_recovery=lambda: SimpleNamespace(protected_run_ids=protected_run_ids),
        set_workload_batch_phase=lambda run_id, phase: connection.execute(
            "UPDATE workload_change_batches SET phase=? WHERE run_id=?", (phase, run_id)
        ),
        mark_recovery_required=mark_recovery_required_in_connection,
        finish_run=finish_run_in_connection,
        administrator="operator",
        password_hash="new-password-hash",
        default_timezone="UTC",
    )


class PlatformBootstrapTests(unittest.TestCase):
    def test_runtime_directories_are_controller_scoped(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            paths = RuntimePaths(base, base / "runs", base / "inventory", base / "variables", base / "runtime", base / "runtime" / "ssh")
            result = ensure_runtime_directories(paths)
            self.assertTrue(result.runs.is_dir())
            self.assertTrue(result.inventories.is_dir())
            self.assertTrue(result.variables.is_dir())

    def test_schema_and_migration_contracts_are_idempotent(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        introspection = SchemaIntrospection(connection)
        self.assertFalse(introspection.table_exists("example"))
        connection.execute("CREATE TABLE example(id INTEGER PRIMARY KEY, value TEXT)")
        self.assertTrue(introspection.table_exists("example"))
        self.assertEqual(introspection.columns("example"), {"id", "value"})
        registry = ensure_migration_ledger(connection, "test_migrations")
        registry.ensure()
        registry.ensure()
        self.assertEqual(registry.applied(), {})

    def test_controller_schema_is_created_outside_application_assembly(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        bootstrap_controller_schema(connection)
        bootstrap_controller_schema(connection)
        schema = SchemaIntrospection(connection)
        self.assertTrue(schema.table_exists("users"))
        self.assertTrue(schema.table_exists("cluster_assignments"))
        self.assertTrue(schema.table_exists("workload_observations"))
        self.assertTrue(schema.table_exists("host_runtime_observations"))
        self.assertTrue(schema.table_exists("audit_events"))

    def test_startup_recovery_uses_injected_domain_contracts(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        bootstrap_controller_schema(connection)
        connection.executescript(
            "CREATE TABLE maintenance_plans(id INTEGER PRIMARY KEY, run_id INTEGER);"
            "INSERT INTO runs(id,kind,target,status,command_json) VALUES(7,'workload','test','running','[]');"
            "INSERT INTO workload_change_batches(run_id,cluster_id,plan_encrypted) VALUES(7,1,'sealed');"
        )
        phases = []
        recoveries = []
        completed = []

        def install_maintenance_schema(_connection):
            return None

        protected = complete_controller_bootstrap(
            connection,
            maintenance_migrations=(install_maintenance_schema,),
            prepare_maintenance_recovery=lambda: SimpleNamespace(protected_run_ids=frozenset({9})),
            set_workload_batch_phase=lambda run_id, phase: phases.append((run_id, phase)),
            mark_recovery_required=lambda _connection, run_ids, message: recoveries.append((run_ids, message)),
            finish_run=lambda _connection, run_id, status, **kwargs: completed.append((run_id, status, kwargs["log_suffix"])),
            administrator="operator",
            password_hash="hashed",
            default_timezone="UTC",
        )
        self.assertEqual(protected, frozenset({9}))
        self.assertEqual(phases, [(7, "rolling_back")])
        self.assertEqual(recoveries[0][0], [7])
        self.assertEqual(completed[0][0:2], (7, "failed"))
        self.assertEqual(connection.execute("SELECT value FROM controller_settings WHERE key='timezone'").fetchone()["value"], "UTC")

    def test_controller_bootstrap_service_runs_callbacks_and_preserves_recovery_artifacts(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            paths = RuntimePaths(base, base / "runs", base / "inventory", base / "variables", base / "runtime", base / "runtime" / "ssh")
            database = sqlite3.connect(":memory:")
            database.row_factory = sqlite3.Row
            calls = []
            (paths.inventories).mkdir(parents=True)
            (paths.variables).mkdir(parents=True)
            (paths.inventories / "run-7.yaml").write_text("inventory", encoding="utf-8")
            (paths.variables / "run-8.yaml").write_text("variables", encoding="utf-8")

            service = ControllerBootstrapService(
                runtime_paths=paths,
                database=lambda: _ConnectionScope(database),
                bootstrap_schema=lambda _connection: calls.append("schema"),
                apply_schema_upgrades=lambda _connection: calls.append("upgrades"),
                complete_bootstrap=lambda _connection: calls.append("complete") or frozenset({7}),
            )
            self.assertEqual(service.run(), frozenset({7}))
            self.assertEqual(calls, ["schema", "upgrades", "complete"])
            self.assertTrue((paths.inventories / "run-7.yaml").exists())
            self.assertFalse((paths.variables / "run-8.yaml").exists())

    def test_repeated_bootstrap_preserves_existing_inventory_and_runs_additive_callbacks_once_per_start(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            paths = RuntimePaths(base, base / "runs", base / "inventory", base / "variables", base / "runtime", base / "runtime" / "ssh")
            database = sqlite3.connect(":memory:")
            database.row_factory = sqlite3.Row
            calls = []

            def upgrades(connection):
                calls.append("upgrade")
                if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='fixture_state'").fetchone():
                    connection.execute("CREATE TABLE fixture_state(value TEXT NOT NULL)")
                if not connection.execute("SELECT 1 FROM fixture_state").fetchone():
                    connection.execute("INSERT INTO fixture_state(value) VALUES('preserved')")

            service = ControllerBootstrapService(
                runtime_paths=paths,
                database=lambda: _ConnectionScope(database),
                bootstrap_schema=bootstrap_controller_schema,
                apply_schema_upgrades=upgrades,
                complete_bootstrap=lambda _connection: frozenset(),
            )
            self.assertEqual(service.run(), frozenset())
            self.assertEqual(service.run(), frozenset())
            self.assertEqual(calls, ["upgrade", "upgrade"])
            self.assertEqual(database.execute("SELECT value FROM fixture_state").fetchone()["value"], "preserved")

    def test_oldest_supported_database_upgrades_without_losing_cluster_or_host_data(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            database_path = base / "control.db"
            with connect(database_path) as connection:
                _create_oldest_supported_schema(connection)

            service = ControllerBootstrapService(
                runtime_paths=_runtime_paths(base),
                database=lambda: connect(database_path),
                bootstrap_schema=bootstrap_controller_schema,
                apply_schema_upgrades=_apply_fixture_upgrades,
                complete_bootstrap=_complete_fixture_bootstrap,
            )
            self.assertEqual(service.run(), frozenset())

            with connect(database_path) as connection:
                schema = SchemaIntrospection(connection)
                self.assertTrue({"ssh_host_key", "ssh_key_id", "zone_id"}.issubset(schema.columns("nodes")))
                self.assertTrue(
                    {"theme_color", "desired_version", "network_defaults_json", "secrets_json"}.issubset(
                        schema.columns("clusters")
                    )
                )
                self.assertTrue({"data_address", "user_address", "network_mode"}.issubset(schema.columns("memberships")))
                node = connection.execute("SELECT name,address,ssh_auth_state FROM nodes WHERE id=1").fetchone()
                self.assertEqual((node["name"], node["address"], node["ssh_auth_state"]), ("legacy-node", "198.51.100.10", "legacy"))
                membership = connection.execute(
                    "SELECT data_address,user_address,network_mode FROM memberships WHERE cluster_id=1 AND node_id=1"
                ).fetchone()
                self.assertEqual((membership["data_address"], membership["user_address"], membership["network_mode"]), (None, "198.51.100.10", "dedicated"))
                cluster = connection.execute(
                    "SELECT desired_version,network_defaults_json,elasticsearch_settings_json,secrets_json FROM clusters WHERE id=1"
                ).fetchone()
                self.assertEqual(cluster["desired_version"], "9.1.0")
                self.assertEqual(json.loads(cluster["network_defaults_json"]), {"mode": "shared"})
                self.assertEqual(
                    json.loads(cluster["elasticsearch_settings_json"]),
                    {"indices.recovery.max_bytes_per_sec": "40mb"},
                )
                self.assertTrue(cluster["secrets_json"].startswith("sealed:"))
                self.assertEqual(
                    connection.execute("SELECT config_json FROM cluster_assignments WHERE id=1").fetchone()["config_json"],
                    "sealed:{}",
                )
                self.assertEqual(
                    connection.execute("SELECT password_hash FROM users WHERE username='operator'").fetchone()["password_hash"],
                    "historic-password-hash",
                )

    def test_partially_migrated_database_fills_missing_defaults_without_overwriting_existing_values(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            database_path = base / "control.db"
            with connect(database_path) as connection:
                _create_oldest_supported_schema(connection)
                connection.executescript("""
                    ALTER TABLE nodes ADD COLUMN ssh_host_key TEXT NOT NULL DEFAULT '';
                    ALTER TABLE clusters ADD COLUMN role_ports_json TEXT NOT NULL DEFAULT '{}';
                    ALTER TABLE clusters ADD COLUMN secrets_json TEXT NOT NULL DEFAULT '{}';
                    ALTER TABLE clusters ADD COLUMN theme_color TEXT;
                    ALTER TABLE memberships ADD COLUMN data_interface TEXT;
                    ALTER TABLE memberships ADD COLUMN data_address TEXT;
                """)
                connection.execute(
                    "UPDATE clusters SET theme_color=?,secrets_json=? WHERE id=1",
                    ("operator-blue", 'sealed:{"monitoring_password":"existing","filebeat_password":"existing"}'),
                )
                connection.execute(
                    "UPDATE memberships SET data_interface='ens-data',data_address='198.51.100.11' WHERE cluster_id=1 AND node_id=1"
                )

            service = ControllerBootstrapService(
                runtime_paths=_runtime_paths(base),
                database=lambda: connect(database_path),
                bootstrap_schema=bootstrap_controller_schema,
                apply_schema_upgrades=_apply_fixture_upgrades,
                complete_bootstrap=_complete_fixture_bootstrap,
            )
            self.assertEqual(service.run(), frozenset())

            with connect(database_path) as connection:
                cluster = connection.execute(
                    "SELECT theme_color,desired_version,network_defaults_json,elasticsearch_settings_json,secrets_json FROM clusters WHERE id=1"
                ).fetchone()
                self.assertEqual(cluster["theme_color"], "operator-blue")
                self.assertEqual(cluster["desired_version"], "9.1.0")
                self.assertEqual(json.loads(cluster["network_defaults_json"]), {"mode": "shared"})
                self.assertEqual(
                    json.loads(cluster["elasticsearch_settings_json"]),
                    {"indices.recovery.max_bytes_per_sec": "40mb"},
                )
                self.assertEqual(
                    cluster["secrets_json"],
                    'sealed:{"monitoring_password":"existing","filebeat_password":"existing"}',
                )
                membership = connection.execute(
                    "SELECT data_interface,data_address,user_interface,user_address,network_mode "
                    "FROM memberships WHERE cluster_id=1 AND node_id=1"
                ).fetchone()
                self.assertEqual(
                    tuple(membership),
                    ("ens-data", "198.51.100.11", None, "198.51.100.10", "dedicated"),
                )

    def test_repeated_durable_startup_preserves_upgraded_configuration_without_duplicate_records(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            database_path = base / "control.db"
            with connect(database_path) as connection:
                _create_oldest_supported_schema(connection)

            service = ControllerBootstrapService(
                runtime_paths=_runtime_paths(base),
                database=lambda: connect(database_path),
                bootstrap_schema=bootstrap_controller_schema,
                apply_schema_upgrades=_apply_fixture_upgrades,
                complete_bootstrap=_complete_fixture_bootstrap,
            )
            service.run()
            with connect(database_path) as connection:
                before = tuple(
                    connection.execute(
                        "SELECT role_ports_json,secrets_json,observability_json,config_json "
                        "FROM clusters JOIN cluster_assignments ON cluster_assignments.cluster_id=clusters.id "
                        "WHERE clusters.id=1"
                    ).fetchone()
                )
            service.run()
            with connect(database_path) as connection:
                after = tuple(
                    connection.execute(
                        "SELECT role_ports_json,secrets_json,observability_json,config_json "
                        "FROM clusters JOIN cluster_assignments ON cluster_assignments.cluster_id=clusters.id "
                        "WHERE clusters.id=1"
                    ).fetchone()
                )
                self.assertEqual(after, before)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM controller_settings WHERE key='timezone'").fetchone()[0],
                    1,
                )

    def test_startup_recovers_interrupted_runs_and_keeps_only_protected_artifacts_across_restarts(self):
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            paths = _runtime_paths(base)
            database_path = base / "control.db"
            with connect(database_path) as connection:
                bootstrap_controller_schema(connection)
                _apply_fixture_upgrades(connection)
                connection.execute(
                    "INSERT INTO nodes(id,name,address,ssh_port,ssh_user) VALUES(1,'node','198.51.100.10',22,'root')"
                )
                connection.execute(
                    "INSERT INTO clusters(id,name,slug,ports_json) VALUES(1,'Cluster','cluster','{\"elasticsearch_http\":9200}')"
                )
                connection.execute(
                    "INSERT INTO runs(id,kind,target,status,command_json) VALUES(7,'workload','cluster','running','[]')"
                )
                connection.execute(
                    "INSERT INTO runs(id,kind,target,status,command_json) VALUES(8,'workload','cluster','queued','[]')"
                )
                connection.execute(
                    "INSERT INTO workload_change_batches(run_id,cluster_id,plan_encrypted) VALUES(7,1,'sealed-plan')"
                )
            paths.inventories.mkdir(parents=True)
            paths.variables.mkdir(parents=True)
            for directory in (paths.inventories, paths.variables):
                (directory / "run-7.yaml").write_text("protected", encoding="utf-8")
                (directory / "run-8.yaml").write_text("discard", encoding="utf-8")

            service = ControllerBootstrapService(
                runtime_paths=paths,
                database=lambda: connect(database_path),
                bootstrap_schema=bootstrap_controller_schema,
                apply_schema_upgrades=_apply_fixture_upgrades,
                complete_bootstrap=lambda connection: _complete_fixture_bootstrap(
                    connection, protected_run_ids=frozenset({7})
                ),
            )
            self.assertEqual(service.run(), frozenset({7}))
            self.assertTrue((paths.inventories / "run-7.yaml").exists())
            self.assertTrue((paths.variables / "run-7.yaml").exists())
            self.assertFalse((paths.inventories / "run-8.yaml").exists())
            self.assertFalse((paths.variables / "run-8.yaml").exists())
            with connect(database_path) as connection:
                self.assertEqual(
                    connection.execute("SELECT status FROM runs WHERE id=7").fetchone()["status"],
                    "recovery_required",
                )
                self.assertEqual(
                    connection.execute("SELECT status FROM runs WHERE id=8").fetchone()["status"],
                    "failed",
                )
                self.assertEqual(
                    connection.execute("SELECT phase FROM workload_change_batches WHERE run_id=7").fetchone()["phase"],
                    "rolling_back",
                )
                first_failure_log = connection.execute("SELECT log FROM runs WHERE id=8").fetchone()["log"]

            self.assertEqual(service.run(), frozenset({7}))
            with connect(database_path) as connection:
                self.assertEqual(
                    connection.execute("SELECT log FROM runs WHERE id=8").fetchone()["log"],
                    first_failure_log,
                )
            self.assertTrue((paths.inventories / "run-7.yaml").exists())
            self.assertTrue((paths.variables / "run-7.yaml").exists())


class _ConnectionScope:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, *_args):
        return False
