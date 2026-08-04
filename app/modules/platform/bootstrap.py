"""Platform-owned process bootstrap and schema introspection contracts.

The application assembly may provide callbacks for legacy migrations, but it
must not need to know how runtime directories or migration ledgers are
created.  This module deliberately contains no feature-domain SQL.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Callable, Iterable

from .config import RuntimePaths
from .migrations import MigrationRegistry, run_registered_migrations as _run_registered_migrations


@dataclass(frozen=True)
class ControllerBootstrapService:
    """Own controller startup ordering and transient artifact cleanup.

    Application assembly supplies feature callbacks through this small public
    contract.  Keeping the callbacks injected prevents the platform layer from
    importing maintenance, workload, or identity implementations while still
    making startup/recovery behavior testable outside ``app.main``.
    """

    runtime_paths: RuntimePaths
    database: Callable[[], sqlite3.Connection]
    bootstrap_schema: Callable[[sqlite3.Connection], None]
    apply_schema_upgrades: Callable[[sqlite3.Connection], None]
    complete_bootstrap: Callable[[sqlite3.Connection], frozenset[int]]
    protected_artifact_pattern: str = "run-*.yaml"

    def run(self) -> frozenset[int]:
        ensure_runtime_directories(self.runtime_paths)
        with self.database() as connection:
            self.bootstrap_schema(connection)
            self.apply_schema_upgrades(connection)
            protected = self.complete_bootstrap(connection)
        self.cleanup_transient_artifacts(protected)
        return protected

    def cleanup_transient_artifacts(self, protected_run_ids: frozenset[int] = frozenset()) -> None:
        """Remove only controller-generated run artifacts not under recovery."""

        for directory in (self.runtime_paths.inventories, self.runtime_paths.variables):
            directory.mkdir(parents=True, exist_ok=True)
            for path in directory.glob(self.protected_artifact_pattern):
                artifact_run = path.name.removeprefix("run-").split("-", 1)[0].split(".", 1)[0]
                if artifact_run.isdigit() and int(artifact_run) in protected_run_ids:
                    continue
                path.unlink(missing_ok=True)


CONTROLLER_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY, password_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS controller_settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS nodes (
  id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, address TEXT NOT NULL,
  ssh_port INTEGER NOT NULL, ssh_user TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
  ssh_host_key TEXT NOT NULL DEFAULT '', ssh_auth_state TEXT NOT NULL DEFAULT 'legacy',
  ssh_key_id TEXT NOT NULL DEFAULT '', candidate_key_id TEXT NOT NULL DEFAULT '',
  legacy_known_hosts_disabled INTEGER NOT NULL DEFAULT 0, zone_id TEXT
);
CREATE TABLE IF NOT EXISTS controller_ssh_keys (
  id INTEGER PRIMARY KEY,
  key_id TEXT UNIQUE NOT NULL,
  algorithm TEXT NOT NULL,
  public_key TEXT NOT NULL,
  private_key_encrypted TEXT NOT NULL,
  source TEXT NOT NULL,
  state TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS assignments (
  node_id INTEGER NOT NULL, role TEXT NOT NULL, config_json TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY(node_id, role)
);
CREATE TABLE IF NOT EXISTS clusters (
  id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, slug TEXT UNIQUE NOT NULL,
  ports_json TEXT NOT NULL, role_ports_json TEXT NOT NULL DEFAULT '{}', secrets_json TEXT NOT NULL DEFAULT '{}',
  observability_json TEXT NOT NULL DEFAULT '{}', zoning_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS memberships (
  cluster_id INTEGER NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
  node_id INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
  network_mode TEXT NOT NULL DEFAULT 'dedicated',
  data_interface TEXT,
  data_address TEXT,
  user_interface TEXT,
  user_address TEXT,
  PRIMARY KEY(cluster_id, node_id)
);
CREATE TABLE IF NOT EXISTS cluster_assignments (
  id INTEGER PRIMARY KEY,
  cluster_id INTEGER NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
  node_id INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
  role TEXT NOT NULL,
  config_json TEXT NOT NULL,
  image_version TEXT,
  state TEXT NOT NULL DEFAULT 'active',
  revision INTEGER NOT NULL DEFAULT 1,
  operation_run_id INTEGER REFERENCES runs(id) ON DELETE SET NULL,
  UNIQUE(cluster_id, node_id, role)
);
CREATE TABLE IF NOT EXISTS workload_change_batches (
  run_id INTEGER PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
  cluster_id INTEGER NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
  plan_encrypted TEXT NOT NULL,
  completed_json TEXT NOT NULL DEFAULT '[]',
  phase TEXT NOT NULL DEFAULT 'applying'
);
CREATE TABLE IF NOT EXISTS workload_observations (
  assignment_id INTEGER PRIMARY KEY REFERENCES cluster_assignments(id) ON DELETE CASCADE,
  image TEXT NOT NULL DEFAULT '', digest TEXT NOT NULL DEFAULT '', version TEXT NOT NULL DEFAULT '',
  running INTEGER NOT NULL DEFAULT 0, cached INTEGER NOT NULL DEFAULT 0, observed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  error TEXT NOT NULL DEFAULT '', filebeat_state TEXT NOT NULL DEFAULT 'disabled',
  filebeat_observed_at TEXT, filebeat_error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, target TEXT NOT NULL, status TEXT NOT NULL,
  command_json TEXT NOT NULL, log TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, finished_at TEXT,
  context_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS host_runtime_observations (
  node_id INTEGER PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
  initialized INTEGER NOT NULL DEFAULT 0,
  reachable INTEGER NOT NULL DEFAULT 0,
  podman_socket_active INTEGER NOT NULL DEFAULT 0,
  os_name TEXT NOT NULL DEFAULT '',
  podman_version TEXT NOT NULL DEFAULT '',
  network_interfaces_json TEXT NOT NULL DEFAULT '{}',
  observed_at TEXT,
  last_error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS cluster_zoning_observations (
  cluster_id INTEGER PRIMARY KEY REFERENCES clusters(id) ON DELETE CASCADE,
  applied_mode TEXT NOT NULL DEFAULT 'disabled',
  applied_zones_json TEXT NOT NULL DEFAULT '[]',
  observed_zones_json TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'pending',
  last_run_id INTEGER REFERENCES runs(id) ON DELETE SET NULL,
  observed_at TEXT,
  last_error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS audit_events (
  id INTEGER PRIMARY KEY,
  username TEXT NOT NULL,
  action TEXT NOT NULL,
  cluster_id INTEGER,
  item_id TEXT NOT NULL DEFAULT '',
  detail TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


@dataclass(frozen=True)
class BootstrapResult:
    data: Path
    runs: Path
    inventories: Path
    variables: Path


def ensure_runtime_directories(paths: RuntimePaths) -> BootstrapResult:
    """Create only controller-owned runtime directories and return them."""

    values = {
        "data": paths.data,
        "runs": paths.data / "runs",
        "inventories": paths.data / "inventory",
        "variables": paths.data / "variables",
    }
    for value in values.values():
        value.mkdir(parents=True, exist_ok=True)
    return BootstrapResult(**values)


def bootstrap_controller_schema(connection: sqlite3.Connection) -> None:
    """Create the canonical controller schema before additive migrations run."""

    connection.executescript(CONTROLLER_SCHEMA)


def apply_controller_schema_upgrades(
    connection: sqlite3.Connection,
    *,
    default_stack_version: str,
    theme_palette: tuple[str, ...],
    network_defaults_json: str,
    elasticsearch_settings_json: str,
    stored_role_ports: Callable[[str, dict], dict],
    log_monitoring_config: Callable[[str], dict],
    open_config: Callable[[str], dict],
    seal_config: Callable[[str], str],
    token_factory: Callable[[int], str],
) -> None:
    """Apply additive controller schema upgrades and normalized defaults.

    Product-specific defaults are injected from application assembly. The
    platform owns the SQLite DDL and preserves every historical migration
    behavior for existing controller databases.
    """

    schema = SchemaIntrospection(connection)
    columns = schema.columns("runs")
    node_columns = schema.columns("nodes")
    cluster_columns = schema.columns("clusters")
    membership_columns = schema.columns("memberships")
    assignment_columns = schema.columns("cluster_assignments")
    observation_columns = schema.columns("workload_observations")
    runtime_columns = schema.columns("host_runtime_observations")
    for column, definition in {
        "ssh_host_key": "TEXT NOT NULL DEFAULT ''",
        "ssh_auth_state": "TEXT NOT NULL DEFAULT 'legacy'",
        "ssh_key_id": "TEXT NOT NULL DEFAULT ''",
        "candidate_key_id": "TEXT NOT NULL DEFAULT ''",
        "legacy_known_hosts_disabled": "INTEGER NOT NULL DEFAULT 0",
        "zone_id": "TEXT",
    }.items():
        if column not in node_columns:
            connection.execute(f"ALTER TABLE nodes ADD COLUMN {column} {definition}")
    if "role_ports_json" not in cluster_columns:
        connection.execute("ALTER TABLE clusters ADD COLUMN role_ports_json TEXT NOT NULL DEFAULT '{}'")
    if "os_name" not in runtime_columns:
        connection.execute("ALTER TABLE host_runtime_observations ADD COLUMN os_name TEXT NOT NULL DEFAULT ''")
    if "network_interfaces_json" not in runtime_columns:
        connection.execute("ALTER TABLE host_runtime_observations ADD COLUMN network_interfaces_json TEXT NOT NULL DEFAULT '{}'")
    if "secrets_json" not in cluster_columns:
        connection.execute("ALTER TABLE clusters ADD COLUMN secrets_json TEXT NOT NULL DEFAULT '{}'")
    for column, definition in {
        "theme_color": "TEXT",
        "desired_version": "TEXT",
        "network_defaults_json": "TEXT NOT NULL DEFAULT '{}'",
        "elasticsearch_settings_json": "TEXT NOT NULL DEFAULT '{}'",
        "observability_json": "TEXT NOT NULL DEFAULT '{}'",
        "zoning_json": "TEXT NOT NULL DEFAULT '{}'",
    }.items():
        if column not in cluster_columns:
            connection.execute(f"ALTER TABLE clusters ADD COLUMN {column} {definition}")
    cluster_defaults = connection.execute(
        "SELECT id,theme_color FROM clusters ORDER BY id"
    ).fetchall()
    used_colors = [row["theme_color"] for row in cluster_defaults if row["theme_color"] is not None]
    for row in cluster_defaults:
        color = row["theme_color"]
        if color is None:
            color = next(
                (item for item in theme_palette if item not in used_colors),
                theme_palette[row["id"] % len(theme_palette)],
            )
            used_colors.append(color)
        connection.execute(
            "UPDATE clusters SET theme_color=COALESCE(theme_color,?),desired_version=COALESCE(desired_version,?),"
            "network_defaults_json=CASE WHEN network_defaults_json='{}' THEN ? ELSE network_defaults_json END,"
            "elasticsearch_settings_json=CASE WHEN elasticsearch_settings_json='{}' THEN ? ELSE elasticsearch_settings_json END "
            "WHERE id=?",
            (color, default_stack_version, network_defaults_json, elasticsearch_settings_json, row["id"]),
        )
    if "context_json" not in columns:
        connection.execute("ALTER TABLE runs ADD COLUMN context_json TEXT NOT NULL DEFAULT '{}' ")
    if "image_version" not in assignment_columns:
        connection.execute("ALTER TABLE cluster_assignments ADD COLUMN image_version TEXT")
    if "revision" not in assignment_columns:
        connection.execute("ALTER TABLE cluster_assignments ADD COLUMN revision INTEGER NOT NULL DEFAULT 1")
    if "operation_run_id" not in assignment_columns:
        connection.execute("ALTER TABLE cluster_assignments ADD COLUMN operation_run_id INTEGER")
    if "network_mode" not in membership_columns:
        connection.execute("ALTER TABLE memberships ADD COLUMN network_mode TEXT NOT NULL DEFAULT 'dedicated'")
    if "cached" not in observation_columns:
        connection.execute("ALTER TABLE workload_observations ADD COLUMN cached INTEGER NOT NULL DEFAULT 0")
    for column, definition in {
        "filebeat_state": "TEXT NOT NULL DEFAULT 'disabled'",
        "filebeat_observed_at": "TEXT",
        "filebeat_error": "TEXT NOT NULL DEFAULT ''",
    }.items():
        if column not in observation_columns:
            connection.execute(f"ALTER TABLE workload_observations ADD COLUMN {column} {definition}")
    for column in ("data_interface", "data_address", "user_interface", "user_address"):
        if column not in membership_columns:
            connection.execute(f"ALTER TABLE memberships ADD COLUMN {column} TEXT")
    if "advertised_address" in membership_columns:
        connection.execute("UPDATE memberships SET user_address=COALESCE(user_address, advertised_address)")
    for row in connection.execute("SELECT id,ports_json,role_ports_json FROM clusters").fetchall():
        legacy_ports = json.loads(row["ports_json"])
        role_ports = stored_role_ports(row["role_ports_json"], legacy_ports)
        encoded = json.dumps(role_ports, sort_keys=True)
        if row["role_ports_json"] != encoded:
            connection.execute("UPDATE clusters SET role_ports_json=? WHERE id=?", (encoded, row["id"]))
    for row in connection.execute("SELECT id,secrets_json FROM clusters").fetchall():
        cluster_secrets = open_config(row["secrets_json"])
        if not cluster_secrets.get("monitoring_password"):
            cluster_secrets["monitoring_password"] = token_factory(24)
            connection.execute(
                "UPDATE clusters SET secrets_json=? WHERE id=?",
                (seal_config(json.dumps(cluster_secrets)), row["id"]),
            )
        if not cluster_secrets.get("filebeat_password"):
            cluster_secrets["filebeat_password"] = token_factory(24)
            connection.execute(
                "UPDATE clusters SET secrets_json=? WHERE id=?",
                (seal_config(json.dumps(cluster_secrets)), row["id"]),
            )
    for row in connection.execute("SELECT id,observability_json FROM clusters").fetchall():
        observability = log_monitoring_config(row["observability_json"])
        encoded = json.dumps(observability, sort_keys=True)
        if row["observability_json"] != encoded:
            connection.execute("UPDATE clusters SET observability_json=? WHERE id=?", (encoded, row["id"]))


def complete_controller_bootstrap(
    connection: sqlite3.Connection,
    *,
    maintenance_migrations: Iterable[Callable[[sqlite3.Connection], None]],
    prepare_maintenance_recovery: Callable[[], object],
    set_workload_batch_phase: Callable[[int, str], None],
    mark_recovery_required: Callable[[sqlite3.Connection, list[int], str], None],
    finish_run: Callable[..., None],
    administrator: str,
    password_hash: str,
    default_timezone: str,
) -> frozenset[int]:
    """Finish startup recovery after schema upgrades have completed.

    Domain-specific lifecycle transitions are supplied as public contracts.
    This keeps the bootstrap service responsible for transaction ordering and
    legacy cleanup without importing maintenance or workload implementations.
    """

    from .migrations import run_migrations

    run_migrations(connection, maintenance_migrations)
    recovery = prepare_maintenance_recovery()
    protected_run_ids = frozenset(int(value) for value in recovery.protected_run_ids)
    connection.execute("DELETE FROM assignments")
    connection.execute(
        "DELETE FROM cluster_assignments WHERE cluster_id NOT IN (SELECT id FROM clusters) "
        "OR node_id NOT IN (SELECT id FROM nodes)"
    )
    connection.execute(
        "DELETE FROM memberships WHERE cluster_id NOT IN (SELECT id FROM clusters) "
        "OR node_id NOT IN (SELECT id FROM nodes)"
    )
    batch_run_rows = connection.execute(
        "SELECT workload_change_batches.run_id "
        "FROM workload_change_batches JOIN runs ON runs.id=workload_change_batches.run_id "
        "WHERE runs.status IN ('queued','running') "
        "AND workload_change_batches.run_id NOT IN "
        "(SELECT run_id FROM maintenance_plans WHERE run_id IS NOT NULL)"
    ).fetchall()
    batch_run_ids = [int(row["run_id"]) for row in batch_run_rows]
    for run_id in batch_run_ids:
        set_workload_batch_phase(run_id, "rolling_back")
    mark_recovery_required(
        connection,
        batch_run_ids,
        "Controller restarted before this workload batch completed; rollback is required.",
    )
    interrupted_run_rows = connection.execute(
        "SELECT id FROM runs WHERE status IN ('queued','running') "
        "AND id NOT IN (SELECT run_id FROM maintenance_plans WHERE run_id IS NOT NULL)"
    ).fetchall()
    for row in interrupted_run_rows:
        finish_run(
            connection,
            int(row["id"]),
            "failed",
            log_suffix="Controller restarted before this run completed.\n",
        )
    if not connection.execute("SELECT 1 FROM users WHERE username=?", (administrator,)).fetchone():
        connection.execute("INSERT INTO users VALUES (?, ?)", (administrator, password_hash))
    connection.execute(
        "INSERT OR IGNORE INTO controller_settings(key,value) VALUES ('timezone',?)",
        (default_timezone,),
    )
    return protected_run_ids


class SchemaIntrospection:
    """Read-only schema contract used by feature migrations."""

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def table_exists(self, table_name: str) -> bool:
        if not table_name or not table_name.replace("_", "").isalnum():
            raise ValueError("Invalid table name")
        row = self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
        return row is not None

    def columns(self, table_name: str) -> frozenset[str]:
        if not self.table_exists(table_name):
            return frozenset()
        return frozenset((row["name"] if hasattr(row, "keys") else row[1]) for row in self.connection.execute(f"PRAGMA table_info({table_name})"))


def ensure_migration_ledger(connection: sqlite3.Connection, table_name: str = "maintenance_schema_migrations") -> MigrationRegistry:
    """Return an ensured platform migration registry."""

    registry = MigrationRegistry(connection, table_name)
    registry.ensure()
    return registry


def run_registered_migrations(connection: sqlite3.Connection, migrations: Iterable[tuple[int, str, str, object]], *, table_name: str = "maintenance_schema_migrations") -> None:
    """Compatibility export for the platform-owned registered migration runner."""

    _run_registered_migrations(connection, migrations, table_name=table_name)
