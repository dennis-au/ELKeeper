from __future__ import annotations

import json
import re
import secrets
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

from app.modules.platform import (
    MigrationRegistry,
    finish_run_in_connection,
    table_exists,
    mark_recovery_required_in_connection,
    statuses_in_connection,
    update_run_status_in_connection,
)
from app.modules.platform import write_event_in_connection
from .repository import ConflictObservation, MaintenanceRepository as MaintenanceReadRepository

from app.modules.maintenance.lifecycle import (
    HOST_TRANSITIONS,
    PLAN_TRANSITIONS,
    STEP_TRANSITIONS,
    HostMaintenanceState,
    LockScope,
    MaintenanceState,
    MaintenanceStepState,
    PlanHashInput,
    SideEffectState,
    canonical_hash,
    canonical_json,
    canonical_plan_hash,
    redact_structure,
    validate_host_transition,
    validate_plan_transition,
    validate_step_transition,
)
from app.modules.maintenance.recovery import (
    MaintenanceStartupRecoveryCoordinator,
    RecoveryDecision,
    RecoveryEvidence,
    StartupRecoveryResult,
    classify_recovery,
)


FOUNDATION_SCHEMA_VERSION = 1
FOUNDATION_SCHEMA_NAME = "phase_0_maintenance_foundation"
FOUNDATION_SCHEMA_CHECKSUM = canonical_hash({
    "version": FOUNDATION_SCHEMA_VERSION,
    "schema": FOUNDATION_SCHEMA_NAME,
})
PROVIDER_SCHEMA_VERSION = 2
PROVIDER_SCHEMA_NAME = "phase_0_provider_ownership"
PROVIDER_SCHEMA_SPEC = {
    "cluster_columns": (
        "provider_type",
        "ownership_state",
        "maintenance_backend",
        "provider_capabilities_json",
        "provider_connection_json",
        "expected_cluster_uuid",
        "provider_revision",
    ),
}
PROVIDER_SCHEMA_CHECKSUM = canonical_hash({
    "version": PROVIDER_SCHEMA_VERSION,
    "schema": PROVIDER_SCHEMA_NAME,
    "spec": PROVIDER_SCHEMA_SPEC,
})
OBSERVATION_SCHEMA_VERSION = 3
OBSERVATION_SCHEMA_NAME = "phase_1_observation_identity"
OBSERVATION_SCHEMA_SPEC = {"host_runtime_columns": ("network_interfaces_json",)}
OBSERVATION_SCHEMA_CHECKSUM = canonical_hash({
    "version": OBSERVATION_SCHEMA_VERSION,
    "schema": OBSERVATION_SCHEMA_NAME,
    "spec": OBSERVATION_SCHEMA_SPEC,
})
SCHEMA_VERSION = OBSERVATION_SCHEMA_VERSION
SCHEMA_NAME = OBSERVATION_SCHEMA_NAME
SCHEMA_CHECKSUM = OBSERVATION_SCHEMA_CHECKSUM
TERMINAL_PLAN_STATES = frozenset({
    MaintenanceState.SUCCEEDED,
    MaintenanceState.FAILED,
    MaintenanceState.CANCELLED,
})


class MaintenanceStoreError(RuntimeError):
    pass


class MigrationDriftError(MaintenanceStoreError):
    pass


class RecordNotFound(MaintenanceStoreError):
    pass


class RevisionConflict(MaintenanceStoreError):
    pass


class IdempotencyConflict(MaintenanceStoreError):
    pass


class OverlappingPlanError(MaintenanceStoreError):
    pass


class LockConflict(MaintenanceStoreError):
    pass


class StaleLockRequiresRecovery(LockConflict):
    pass


class LockOwnershipError(MaintenanceStoreError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_timestamp(value: datetime | None = None) -> str:
    value = value or utc_now()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _json(value: Any) -> str:
    return canonical_json(redact_structure(value))


def _load_json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    return json.loads(value)


@dataclass(frozen=True)
class PolicyRecord:
    cluster_id: int
    policy: Mapping[str, Any]
    revision: int
    updated_by: str
    updated_at: str


@dataclass(frozen=True)
class PlanRecord:
    id: str
    run_id: int | None
    operation_kind: str
    target_node_id: int | None
    target_cluster_id: int | None
    target_assignment_id: int | None
    plan: Mapping[str, Any]
    observation: Mapping[str, Any]
    plan_hash: str
    idempotency_key: str
    expected_policy_revision: int | None
    expected_assignment_revision: int | None
    observed_at: str | None
    target_manifest: Mapping[str, Any]
    lifecycle_state: MaintenanceState
    state_revision: int
    requested_by: str
    approved_at: str | None
    created_at: str
    expires_at: str
    completed_at: str | None
    retention_until: str | None


@dataclass(frozen=True)
class StepRecord:
    id: int
    plan_id: str
    step_key: str
    sequence: int
    affected_cluster_id: int | None
    affected_assignment_id: int | None
    affected_node_id: int | None
    elasticsearch_node_id: str | None
    step_kind: str
    state: MaintenanceStepState
    state_revision: int
    attempt_count: int
    before_observation: Mapping[str, Any]
    after_observation: Mapping[str, Any]
    last_error_category: str | None
    resumability_decision: str | None
    started_at: str | None
    finished_at: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class CheckpointRecord:
    id: int
    plan_id: str
    step_id: int | None
    checkpoint_key: str
    sequence: int
    side_effect_state: SideEffectState
    payload: Mapping[str, Any]
    observation: Mapping[str, Any]
    recovery_evidence: Mapping[str, Any]
    recovery_classification: str | None
    recovery_reason_code: str | None
    resumable: bool | None
    classification_revision: int
    created_at: str
    classified_at: str | None


@dataclass(frozen=True)
class HostStateRecord:
    node_id: int
    state: HostMaintenanceState
    active_plan_id: str | None
    state_revision: int
    entered_at: str
    updated_at: str


@dataclass(frozen=True)
class LockRequest:
    scope: LockScope | str
    identifier: str | int

    def normalized(self) -> tuple[str, str]:
        return LockScope(self.scope).value, str(self.identifier)


@dataclass(frozen=True)
class LockRecord:
    id: int
    scope: LockScope
    identifier: str
    owner_plan_id: str
    run_id: int | None
    owner_token: str = field(repr=False)
    acquired_at: str
    heartbeat_at: str
    expires_at: str
    released_at: str | None
    stale_released_at: str | None
    release_reason: str | None
    release_observation: Mapping[str, Any]
    recovered_by: str | None

    def expired(self, now: datetime | None = None) -> bool:
        return parse_timestamp(self.expires_at) <= (now or utc_now())


@dataclass(frozen=True)
class StartupRecovery:
    protected_run_ids: frozenset[int]
    discovered_plan_ids: tuple[str, ...]
    transitioned_plan_ids: tuple[str, ...]
    classifications: tuple[StartupRecoveryResult, ...] = ()


def _ensure_columns(connection: sqlite3.Connection, table: str, columns: Mapping[str, str]) -> None:
    existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
    for name, definition in columns.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _create_tables(connection: sqlite3.Connection) -> None:
    statements = (
        """
        CREATE TABLE IF NOT EXISTS maintenance_policies (
          cluster_id INTEGER PRIMARY KEY REFERENCES clusters(id) ON DELETE CASCADE,
          policy_json TEXT NOT NULL DEFAULT '{}',
          revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
          updated_by TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS maintenance_plans (
          id TEXT PRIMARY KEY,
          run_id INTEGER REFERENCES runs(id) ON DELETE SET NULL,
          operation_kind TEXT NOT NULL,
          target_node_id INTEGER REFERENCES nodes(id) ON DELETE SET NULL,
          target_cluster_id INTEGER REFERENCES clusters(id) ON DELETE SET NULL,
          target_assignment_id INTEGER REFERENCES cluster_assignments(id) ON DELETE SET NULL,
          plan_json TEXT NOT NULL,
          observation_json TEXT NOT NULL DEFAULT '{}',
          plan_hash TEXT NOT NULL,
          idempotency_key TEXT NOT NULL UNIQUE,
          expected_policy_revision INTEGER,
          expected_assignment_revision INTEGER,
          observed_at TEXT,
          target_manifest_json TEXT NOT NULL DEFAULT '{}',
          lifecycle_state TEXT NOT NULL,
          state_revision INTEGER NOT NULL DEFAULT 1 CHECK(state_revision > 0),
          requested_by TEXT NOT NULL,
          approved_at TEXT,
          created_at TEXT NOT NULL,
          expires_at TEXT NOT NULL,
          completed_at TEXT,
          retention_until TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS maintenance_steps (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          plan_id TEXT NOT NULL REFERENCES maintenance_plans(id) ON DELETE CASCADE,
          step_key TEXT NOT NULL,
          sequence INTEGER NOT NULL CHECK(sequence >= 0),
          affected_cluster_id INTEGER REFERENCES clusters(id) ON DELETE SET NULL,
          affected_assignment_id INTEGER REFERENCES cluster_assignments(id) ON DELETE SET NULL,
          affected_node_id INTEGER REFERENCES nodes(id) ON DELETE SET NULL,
          elasticsearch_node_id TEXT,
          step_kind TEXT NOT NULL,
          state TEXT NOT NULL DEFAULT 'pending',
          state_revision INTEGER NOT NULL DEFAULT 1 CHECK(state_revision > 0),
          attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
          before_observation_json TEXT NOT NULL DEFAULT '{}',
          after_observation_json TEXT NOT NULL DEFAULT '{}',
          last_error_category TEXT,
          resumability_decision TEXT,
          started_at TEXT,
          finished_at TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(plan_id, step_key),
          UNIQUE(plan_id, sequence)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS maintenance_checkpoints (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          plan_id TEXT NOT NULL REFERENCES maintenance_plans(id) ON DELETE CASCADE,
          step_id INTEGER REFERENCES maintenance_steps(id) ON DELETE CASCADE,
          checkpoint_key TEXT NOT NULL,
          sequence INTEGER NOT NULL CHECK(sequence >= 0),
          side_effect_state TEXT NOT NULL,
          payload_json TEXT NOT NULL DEFAULT '{}',
          observation_json TEXT NOT NULL DEFAULT '{}',
          recovery_evidence_json TEXT NOT NULL DEFAULT '{}',
          recovery_classification TEXT,
          recovery_reason_code TEXT,
          resumable INTEGER,
          classification_revision INTEGER NOT NULL DEFAULT 1 CHECK(classification_revision > 0),
          created_at TEXT NOT NULL,
          classified_at TEXT,
          UNIQUE(plan_id, checkpoint_key),
          UNIQUE(plan_id, sequence)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS host_maintenance_state (
          node_id INTEGER PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
          state TEXT NOT NULL DEFAULT 'available',
          active_plan_id TEXT REFERENCES maintenance_plans(id) ON DELETE SET NULL,
          state_revision INTEGER NOT NULL DEFAULT 1 CHECK(state_revision > 0),
          entered_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS maintenance_locks (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          scope_kind TEXT NOT NULL,
          scope_id TEXT NOT NULL,
          owner_plan_id TEXT NOT NULL REFERENCES maintenance_plans(id) ON DELETE CASCADE,
          run_id INTEGER REFERENCES runs(id) ON DELETE SET NULL,
          owner_token TEXT NOT NULL,
          acquired_at TEXT NOT NULL,
          heartbeat_at TEXT NOT NULL,
          expires_at TEXT NOT NULL,
          released_at TEXT,
          stale_released_at TEXT,
          release_reason TEXT,
          release_observation_json TEXT NOT NULL DEFAULT '{}',
          recovered_by TEXT
        )
        """,
    )
    for statement in statements:
        connection.execute(statement)


def _repair_partial_schema(connection: sqlite3.Connection) -> None:
    _ensure_columns(connection, "maintenance_policies", {
        "cluster_id": "INTEGER",
        "policy_json": "TEXT NOT NULL DEFAULT '{}'",
        "revision": "INTEGER NOT NULL DEFAULT 1",
        "updated_by": "TEXT NOT NULL DEFAULT ''",
        "updated_at": "TEXT NOT NULL DEFAULT ''",
    })
    _ensure_columns(connection, "maintenance_plans", {
        "id": "TEXT",
        "run_id": "INTEGER",
        "operation_kind": "TEXT NOT NULL DEFAULT ''",
        "target_node_id": "INTEGER",
        "target_cluster_id": "INTEGER",
        "target_assignment_id": "INTEGER",
        "plan_json": "TEXT NOT NULL DEFAULT '{}'",
        "observation_json": "TEXT NOT NULL DEFAULT '{}'",
        "plan_hash": "TEXT NOT NULL DEFAULT ''",
        "idempotency_key": "TEXT NOT NULL DEFAULT ''",
        "expected_policy_revision": "INTEGER",
        "expected_assignment_revision": "INTEGER",
        "observed_at": "TEXT",
        "target_manifest_json": "TEXT NOT NULL DEFAULT '{}'",
        "lifecycle_state": "TEXT NOT NULL DEFAULT 'draft'",
        "state_revision": "INTEGER NOT NULL DEFAULT 1",
        "requested_by": "TEXT NOT NULL DEFAULT ''",
        "approved_at": "TEXT",
        "created_at": "TEXT NOT NULL DEFAULT ''",
        "expires_at": "TEXT NOT NULL DEFAULT ''",
        "completed_at": "TEXT",
        "retention_until": "TEXT",
    })
    _ensure_columns(connection, "maintenance_steps", {
        "plan_id": "TEXT",
        "step_key": "TEXT NOT NULL DEFAULT ''",
        "sequence": "INTEGER NOT NULL DEFAULT 0",
        "affected_cluster_id": "INTEGER",
        "affected_assignment_id": "INTEGER",
        "affected_node_id": "INTEGER",
        "elasticsearch_node_id": "TEXT",
        "step_kind": "TEXT NOT NULL DEFAULT ''",
        "state": "TEXT NOT NULL DEFAULT 'pending'",
        "state_revision": "INTEGER NOT NULL DEFAULT 1",
        "attempt_count": "INTEGER NOT NULL DEFAULT 0",
        "before_observation_json": "TEXT NOT NULL DEFAULT '{}'",
        "after_observation_json": "TEXT NOT NULL DEFAULT '{}'",
        "last_error_category": "TEXT",
        "resumability_decision": "TEXT",
        "started_at": "TEXT",
        "finished_at": "TEXT",
        "created_at": "TEXT NOT NULL DEFAULT ''",
        "updated_at": "TEXT NOT NULL DEFAULT ''",
    })
    _ensure_columns(connection, "maintenance_checkpoints", {
        "plan_id": "TEXT",
        "step_id": "INTEGER",
        "checkpoint_key": "TEXT NOT NULL DEFAULT ''",
        "sequence": "INTEGER NOT NULL DEFAULT 0",
        "side_effect_state": "TEXT NOT NULL DEFAULT 'not_started'",
        "payload_json": "TEXT NOT NULL DEFAULT '{}'",
        "observation_json": "TEXT NOT NULL DEFAULT '{}'",
        "recovery_evidence_json": "TEXT NOT NULL DEFAULT '{}'",
        "recovery_classification": "TEXT",
        "recovery_reason_code": "TEXT",
        "resumable": "INTEGER",
        "classification_revision": "INTEGER NOT NULL DEFAULT 1",
        "created_at": "TEXT NOT NULL DEFAULT ''",
        "classified_at": "TEXT",
    })
    _ensure_columns(connection, "host_maintenance_state", {
        "node_id": "INTEGER",
        "state": "TEXT NOT NULL DEFAULT 'available'",
        "active_plan_id": "TEXT",
        "state_revision": "INTEGER NOT NULL DEFAULT 1",
        "entered_at": "TEXT NOT NULL DEFAULT ''",
        "updated_at": "TEXT NOT NULL DEFAULT ''",
    })
    _ensure_columns(connection, "maintenance_locks", {
        "scope_kind": "TEXT NOT NULL DEFAULT ''",
        "scope_id": "TEXT NOT NULL DEFAULT ''",
        "owner_plan_id": "TEXT",
        "run_id": "INTEGER",
        "owner_token": "TEXT NOT NULL DEFAULT ''",
        "acquired_at": "TEXT NOT NULL DEFAULT ''",
        "heartbeat_at": "TEXT NOT NULL DEFAULT ''",
        "expires_at": "TEXT NOT NULL DEFAULT ''",
        "released_at": "TEXT",
        "stale_released_at": "TEXT",
        "release_reason": "TEXT",
        "release_observation_json": "TEXT NOT NULL DEFAULT '{}'",
        "recovered_by": "TEXT",
    })


def _transition_sql(transitions: Mapping[Any, frozenset[Any]], old_column: str, new_column: str) -> str:
    clauses = []
    for current, targets in transitions.items():
        if targets:
            target_values = ",".join(f"'{target.value}'" for target in sorted(targets, key=lambda item: item.value))
            clauses.append(f"({old_column}='{current.value}' AND {new_column} IN ({target_values}))")
    return " OR ".join(clauses)


def _create_indexes_and_triggers(connection: sqlite3.Connection) -> None:
    statements = (
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_maintenance_plans_idempotency ON maintenance_plans(idempotency_key)",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_maintenance_steps_key ON maintenance_steps(plan_id,step_key)",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_maintenance_steps_sequence ON maintenance_steps(plan_id,sequence)",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_maintenance_checkpoints_key ON maintenance_checkpoints(plan_id,checkpoint_key)",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_maintenance_checkpoints_sequence ON maintenance_checkpoints(plan_id,sequence)",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_maintenance_locks_active_scope ON maintenance_locks(scope_kind,scope_id) WHERE released_at IS NULL",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_maintenance_active_node_plan ON maintenance_plans(target_node_id) WHERE target_node_id IS NOT NULL AND lifecycle_state IN ('ready','executing','paused','recovery_required')",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_maintenance_active_cluster_plan ON maintenance_plans(target_cluster_id) WHERE target_cluster_id IS NOT NULL AND lifecycle_state IN ('ready','executing','paused','recovery_required')",
        "CREATE UNIQUE INDEX IF NOT EXISTS ux_maintenance_active_assignment_plan ON maintenance_plans(target_assignment_id) WHERE target_assignment_id IS NOT NULL AND lifecycle_state IN ('ready','executing','paused','recovery_required')",
        "CREATE INDEX IF NOT EXISTS ix_maintenance_plans_state ON maintenance_plans(lifecycle_state,created_at)",
        "CREATE INDEX IF NOT EXISTS ix_maintenance_steps_plan_state ON maintenance_steps(plan_id,state,sequence)",
        "CREATE INDEX IF NOT EXISTS ix_maintenance_locks_owner ON maintenance_locks(owner_plan_id,released_at)",
        "CREATE INDEX IF NOT EXISTS ix_maintenance_locks_expiry ON maintenance_locks(expires_at) WHERE released_at IS NULL",
    )
    for statement in statements:
        connection.execute(statement)

    enum_triggers = (
        ("maintenance_plan_state_insert", "maintenance_plans", "lifecycle_state", tuple(item.value for item in MaintenanceState)),
        ("maintenance_step_state_insert", "maintenance_steps", "state", tuple(item.value for item in MaintenanceStepState)),
        ("maintenance_host_state_insert", "host_maintenance_state", "state", tuple(item.value for item in HostMaintenanceState)),
        ("maintenance_lock_scope_insert", "maintenance_locks", "scope_kind", tuple(item.value for item in LockScope)),
        ("maintenance_checkpoint_effect_insert", "maintenance_checkpoints", "side_effect_state", tuple(item.value for item in SideEffectState)),
    )
    for name, table, column, values in enum_triggers:
        allowed = ",".join(f"'{value}'" for value in values)
        connection.execute(f"""
            CREATE TRIGGER IF NOT EXISTS {name}
            BEFORE INSERT ON {table}
            WHEN NEW.{column} NOT IN ({allowed})
            BEGIN SELECT RAISE(ABORT, 'invalid {column}'); END
        """)
        connection.execute(f"""
            CREATE TRIGGER IF NOT EXISTS {name.replace('_insert', '_update')}
            BEFORE UPDATE OF {column} ON {table}
            WHEN NEW.{column} NOT IN ({allowed})
            BEGIN SELECT RAISE(ABORT, 'invalid {column}'); END
        """)

    plan_transitions = _transition_sql(PLAN_TRANSITIONS, "OLD.lifecycle_state", "NEW.lifecycle_state")
    step_transitions = _transition_sql(STEP_TRANSITIONS, "OLD.state", "NEW.state")
    host_transitions = _transition_sql(HOST_TRANSITIONS, "OLD.state", "NEW.state")
    connection.execute(f"""
        CREATE TRIGGER IF NOT EXISTS maintenance_plan_legal_transition
        BEFORE UPDATE OF lifecycle_state ON maintenance_plans
        WHEN OLD.lifecycle_state <> NEW.lifecycle_state AND NOT ({plan_transitions})
        BEGIN SELECT RAISE(ABORT, 'illegal maintenance plan transition'); END
    """)
    connection.execute(f"""
        CREATE TRIGGER IF NOT EXISTS maintenance_step_legal_transition
        BEFORE UPDATE OF state ON maintenance_steps
        WHEN OLD.state <> NEW.state AND NOT ({step_transitions})
        BEGIN SELECT RAISE(ABORT, 'illegal maintenance step transition'); END
    """)
    connection.execute(f"""
        CREATE TRIGGER IF NOT EXISTS maintenance_host_legal_transition
        BEFORE UPDATE OF state ON host_maintenance_state
        WHEN OLD.state <> NEW.state AND NOT ({host_transitions})
        BEGIN SELECT RAISE(ABORT, 'illegal host maintenance transition'); END
    """)
    connection.execute("""
        CREATE TRIGGER IF NOT EXISTS maintenance_plan_immutable_content
        BEFORE UPDATE ON maintenance_plans
        WHEN OLD.operation_kind <> NEW.operation_kind
          OR COALESCE(OLD.target_node_id,-1) <> COALESCE(NEW.target_node_id,-1)
          OR COALESCE(OLD.target_cluster_id,-1) <> COALESCE(NEW.target_cluster_id,-1)
          OR COALESCE(OLD.target_assignment_id,-1) <> COALESCE(NEW.target_assignment_id,-1)
          OR OLD.plan_json <> NEW.plan_json
          OR OLD.observation_json <> NEW.observation_json
          OR OLD.plan_hash <> NEW.plan_hash
          OR OLD.idempotency_key <> NEW.idempotency_key
          OR COALESCE(OLD.expected_policy_revision,-1) <> COALESCE(NEW.expected_policy_revision,-1)
          OR COALESCE(OLD.expected_assignment_revision,-1) <> COALESCE(NEW.expected_assignment_revision,-1)
          OR COALESCE(OLD.observed_at,'') <> COALESCE(NEW.observed_at,'')
          OR OLD.target_manifest_json <> NEW.target_manifest_json
        BEGIN SELECT RAISE(ABORT, 'maintenance plan content is immutable'); END
    """)
    connection.execute("""
        CREATE TRIGGER IF NOT EXISTS maintenance_checkpoint_immutable_content
        BEFORE UPDATE ON maintenance_checkpoints
        WHEN OLD.plan_id <> NEW.plan_id
          OR COALESCE(OLD.step_id,-1) <> COALESCE(NEW.step_id,-1)
          OR OLD.checkpoint_key <> NEW.checkpoint_key
          OR OLD.sequence <> NEW.sequence
          OR OLD.side_effect_state <> NEW.side_effect_state
          OR OLD.payload_json <> NEW.payload_json
          OR OLD.observation_json <> NEW.observation_json
        BEGIN SELECT RAISE(ABORT, 'maintenance checkpoint content is immutable'); END
    """)


def _add_provider_ownership_columns(connection: sqlite3.Connection) -> None:
    _ensure_columns(connection, "clusters", {
        "provider_type": "TEXT NOT NULL DEFAULT 'native_podman'",
        "ownership_state": "TEXT NOT NULL DEFAULT 'verified'",
        "maintenance_backend": "TEXT NOT NULL DEFAULT 'documented_rolling'",
        "provider_capabilities_json": "TEXT NOT NULL DEFAULT '{}'",
        "provider_connection_json": "TEXT NOT NULL DEFAULT '{}'",
        "expected_cluster_uuid": "TEXT",
        "provider_revision": "INTEGER NOT NULL DEFAULT 1",
    })
    validations = (
        (
            "cluster_provider_type",
            "provider_type",
            ("native_podman", "adopted_podman", "external_api", "eck_endpoint"),
        ),
        (
            "cluster_ownership_state",
            "ownership_state",
            ("verified", "unverified", "read_only"),
        ),
        (
            "cluster_maintenance_backend",
            "maintenance_backend",
            ("documented_rolling", "node_shutdown_api", "none"),
        ),
    )
    for name, column, values in validations:
        allowed = ",".join(f"'{value}'" for value in values)
        connection.execute(f"""
            CREATE TRIGGER IF NOT EXISTS maintenance_{name}_insert
            BEFORE INSERT ON clusters
            WHEN NEW.{column} NOT IN ({allowed})
            BEGIN SELECT RAISE(ABORT, 'invalid {column}'); END
        """)
        connection.execute(f"""
            CREATE TRIGGER IF NOT EXISTS maintenance_{name}_update
            BEFORE UPDATE OF {column} ON clusters
            WHEN NEW.{column} NOT IN ({allowed})
            BEGIN SELECT RAISE(ABORT, 'invalid {column}'); END
        """)
    connection.execute("""
        CREATE TRIGGER IF NOT EXISTS maintenance_cluster_provider_revision_insert
        BEFORE INSERT ON clusters
        WHEN NEW.provider_revision < 1
        BEGIN SELECT RAISE(ABORT, 'invalid provider_revision'); END
    """)
    connection.execute("""
        CREATE TRIGGER IF NOT EXISTS maintenance_cluster_provider_revision_update
        BEFORE UPDATE OF provider_revision ON clusters
        WHEN NEW.provider_revision < 1
        BEGIN SELECT RAISE(ABORT, 'invalid provider_revision'); END
    """)


def _add_observation_identity_columns(connection: sqlite3.Connection) -> None:
    if not table_exists(connection, "host_runtime_observations"):
        return
    _ensure_columns(connection, "host_runtime_observations", {
        "network_interfaces_json": "TEXT NOT NULL DEFAULT '{}'",
    })


MAINTENANCE_MIGRATIONS = (
    (
        FOUNDATION_SCHEMA_VERSION,
        FOUNDATION_SCHEMA_NAME,
        FOUNDATION_SCHEMA_CHECKSUM,
        lambda connection: (
            _create_tables(connection),
            _repair_partial_schema(connection),
            _create_indexes_and_triggers(connection),
        ),
    ),
    (
        PROVIDER_SCHEMA_VERSION,
        PROVIDER_SCHEMA_NAME,
        PROVIDER_SCHEMA_CHECKSUM,
        _add_provider_ownership_columns,
    ),
    (
        OBSERVATION_SCHEMA_VERSION,
        OBSERVATION_SCHEMA_NAME,
        OBSERVATION_SCHEMA_CHECKSUM,
        _add_observation_identity_columns,
    ),
)


def _reconcile_installed_schema(connection: sqlite3.Connection) -> None:
    """Repair legacy structural gaps without changing the migration ledger.

    Registered migrations are transactional in current releases, but databases
    created by earlier installers can carry a recorded version alongside an
    incomplete table, index, or trigger set.  Re-running these idempotent
    structural operations makes the durable schema safe to reopen after a
    restore or interrupted legacy startup while preserving the recorded
    migration history.
    """

    _create_tables(connection)
    _repair_partial_schema(connection)
    _create_indexes_and_triggers(connection)
    _add_provider_ownership_columns(connection)
    _add_observation_identity_columns(connection)


def install_maintenance_schema(connection: sqlite3.Connection) -> int:
    connection.execute("SAVEPOINT maintenance_schema_install")
    try:
        registry = MigrationRegistry(connection, "maintenance_schema_migrations")
        registry.ensure()
        applied_rows = registry.applied()
        known_versions = {version for version, _name, _checksum, _apply in MAINTENANCE_MIGRATIONS}
        unknown_versions = sorted(set(applied_rows) - known_versions)
        if unknown_versions:
            raise MigrationDriftError(
                "Unknown maintenance migration versions: " + ", ".join(map(str, unknown_versions))
            )
        for version, name, checksum, _apply in MAINTENANCE_MIGRATIONS:
            applied = applied_rows.get(version)
            if applied and (applied["name"] != name or applied["checksum"] != checksum):
                raise MigrationDriftError(f"Maintenance migration {version} checksum does not match")
        for version, name, checksum, apply_migration in MAINTENANCE_MIGRATIONS:
            if version in applied_rows:
                continue
            apply_migration(connection)
            registry.record(version, name, checksum, iso_timestamp())
        _reconcile_installed_schema(connection)
        connection.execute("RELEASE SAVEPOINT maintenance_schema_install")
        return SCHEMA_VERSION
    except Exception:
        connection.execute("ROLLBACK TO SAVEPOINT maintenance_schema_install")
        connection.execute("RELEASE SAVEPOINT maintenance_schema_install")
        raise


def _policy_record(row: sqlite3.Row) -> PolicyRecord:
    return PolicyRecord(row["cluster_id"], _load_json(row["policy_json"], {}), row["revision"], row["updated_by"], row["updated_at"])


def _plan_record(row: sqlite3.Row) -> PlanRecord:
    return PlanRecord(
        id=row["id"],
        run_id=row["run_id"],
        operation_kind=row["operation_kind"],
        target_node_id=row["target_node_id"],
        target_cluster_id=row["target_cluster_id"],
        target_assignment_id=row["target_assignment_id"],
        plan=_load_json(row["plan_json"], {}),
        observation=_load_json(row["observation_json"], {}),
        plan_hash=row["plan_hash"],
        idempotency_key=row["idempotency_key"],
        expected_policy_revision=row["expected_policy_revision"],
        expected_assignment_revision=row["expected_assignment_revision"],
        observed_at=row["observed_at"],
        target_manifest=_load_json(row["target_manifest_json"], {}),
        lifecycle_state=MaintenanceState(row["lifecycle_state"]),
        state_revision=row["state_revision"],
        requested_by=row["requested_by"],
        approved_at=row["approved_at"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        completed_at=row["completed_at"],
        retention_until=row["retention_until"],
    )


def _step_record(row: sqlite3.Row) -> StepRecord:
    return StepRecord(
        id=row["id"], plan_id=row["plan_id"], step_key=row["step_key"], sequence=row["sequence"],
        affected_cluster_id=row["affected_cluster_id"], affected_assignment_id=row["affected_assignment_id"],
        affected_node_id=row["affected_node_id"], elasticsearch_node_id=row["elasticsearch_node_id"],
        step_kind=row["step_kind"], state=MaintenanceStepState(row["state"]), state_revision=row["state_revision"],
        attempt_count=row["attempt_count"], before_observation=_load_json(row["before_observation_json"], {}),
        after_observation=_load_json(row["after_observation_json"], {}), last_error_category=row["last_error_category"],
        resumability_decision=row["resumability_decision"], started_at=row["started_at"], finished_at=row["finished_at"],
        created_at=row["created_at"], updated_at=row["updated_at"],
    )


def _checkpoint_record(row: sqlite3.Row) -> CheckpointRecord:
    return CheckpointRecord(
        id=row["id"], plan_id=row["plan_id"], step_id=row["step_id"], checkpoint_key=row["checkpoint_key"],
        sequence=row["sequence"], side_effect_state=SideEffectState(row["side_effect_state"]),
        payload=_load_json(row["payload_json"], {}), observation=_load_json(row["observation_json"], {}),
        recovery_evidence=_load_json(row["recovery_evidence_json"], {}),
        recovery_classification=row["recovery_classification"], recovery_reason_code=row["recovery_reason_code"],
        resumable=None if row["resumable"] is None else bool(row["resumable"]),
        classification_revision=row["classification_revision"], created_at=row["created_at"], classified_at=row["classified_at"],
    )


def _host_record(row: sqlite3.Row) -> HostStateRecord:
    return HostStateRecord(row["node_id"], HostMaintenanceState(row["state"]), row["active_plan_id"], row["state_revision"], row["entered_at"], row["updated_at"])


def _lock_record(row: sqlite3.Row) -> LockRecord:
    return LockRecord(
        id=row["id"], scope=LockScope(row["scope_kind"]), identifier=row["scope_id"], owner_plan_id=row["owner_plan_id"],
        run_id=row["run_id"], owner_token=row["owner_token"], acquired_at=row["acquired_at"], heartbeat_at=row["heartbeat_at"],
        expires_at=row["expires_at"], released_at=row["released_at"], stale_released_at=row["stale_released_at"],
        release_reason=row["release_reason"], release_observation=_load_json(row["release_observation_json"], {}),
        recovered_by=row["recovered_by"],
    )


class MaintenanceRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        self.connection.row_factory = sqlite3.Row

    def get_policy(self, cluster_id: int) -> PolicyRecord | None:
        row = self.connection.execute("SELECT * FROM maintenance_policies WHERE cluster_id=?", (cluster_id,)).fetchone()
        return _policy_record(row) if row else None

    def put_policy(
        self,
        cluster_id: int,
        policy: Mapping[str, Any],
        updated_by: str,
        expected_revision: int | None = None,
    ) -> PolicyRecord:
        current = self.get_policy(cluster_id)
        now = iso_timestamp()
        if current is None:
            if expected_revision not in (None, 0):
                raise RevisionConflict("Maintenance policy does not exist at the expected revision")
            self.connection.execute(
                "INSERT INTO maintenance_policies(cluster_id,policy_json,revision,updated_by,updated_at) VALUES(?,?,?,?,?)",
                (cluster_id, _json(policy), 1, updated_by, now),
            )
        else:
            if expected_revision is None or current.revision != expected_revision:
                raise RevisionConflict("Maintenance policy revision changed")
            result = self.connection.execute(
                "UPDATE maintenance_policies SET policy_json=?,revision=revision+1,updated_by=?,updated_at=? WHERE cluster_id=? AND revision=?",
                (_json(policy), updated_by, now, cluster_id, expected_revision),
            )
            if result.rowcount != 1:
                raise RevisionConflict("Maintenance policy revision changed")
        return self.get_policy(cluster_id)  # type: ignore[return-value]

    def mark_run_status(
        self,
        run_id: int,
        status: str,
        *,
        finished_at: str | None = None,
        log_suffix: str = "",
    ) -> None:
        """Update a maintenance-attached run through the platform contract."""

        update_run_status_in_connection(
            self.connection,
            run_id,
            status,
            finished_at=finished_at,
            log_suffix=log_suffix,
        )

    def create_plan(
        self,
        *,
        operation_kind: str,
        plan: Mapping[str, Any],
        idempotency_key: str,
        requested_by: str,
        expires_at: datetime,
        observation: Mapping[str, Any] | None = None,
        target_node_id: int | None = None,
        target_cluster_id: int | None = None,
        target_assignment_id: int | None = None,
        expected_policy_revision: int | None = None,
        expected_assignment_revision: int | None = None,
        observed_at: str | None = None,
        target_manifest: Mapping[str, Any] | None = None,
        initial_state: MaintenanceState | str = MaintenanceState.DRAFT,
        run_id: int | None = None,
        retention_until: datetime | None = None,
        plan_id: str | None = None,
        authoritative_plan_hash: str | None = None,
    ) -> PlanRecord:
        if not idempotency_key.strip():
            raise ValueError("idempotency_key must not be blank")
        redacted_plan = redact_structure(plan)
        redacted_observation = redact_structure(observation or {})
        redacted_manifest = redact_structure(target_manifest or {})
        if authoritative_plan_hash is not None:
            if not re.fullmatch(r"[0-9a-f]{64}", authoritative_plan_hash):
                raise ValueError("authoritative_plan_hash must be a lowercase SHA-256 digest")
            compiled_payload = dict(redacted_plan)
            embedded_hash = compiled_payload.pop("plan_hash", None)
            if embedded_hash != authoritative_plan_hash or canonical_hash(compiled_payload) != authoritative_plan_hash:
                raise ValueError("authoritative_plan_hash does not match the compiled maintenance plan")
            plan_hash = authoritative_plan_hash
        else:
            plan_hash = canonical_plan_hash(PlanHashInput(
                operation_kind=operation_kind,
                plan=redacted_plan,
                observation=redacted_observation,
                target_node_id=target_node_id,
                target_cluster_id=target_cluster_id,
                target_assignment_id=target_assignment_id,
                expected_policy_revision=expected_policy_revision,
                expected_assignment_revision=expected_assignment_revision,
                observed_at=observed_at,
                target_manifest=redacted_manifest,
            ))
        existing = self.connection.execute("SELECT * FROM maintenance_plans WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        if existing:
            record = _plan_record(existing)
            if record.plan_hash != plan_hash:
                raise IdempotencyConflict("Idempotency key was already used for a different maintenance plan")
            return record
        state = MaintenanceState(initial_state)
        if state not in {MaintenanceState.DRAFT, MaintenanceState.READY, MaintenanceState.BLOCKED}:
            raise ValueError("A new maintenance plan must start as draft, ready, or blocked")
        created_at = iso_timestamp()
        try:
            self.connection.execute("""
                INSERT INTO maintenance_plans(
                  id,run_id,operation_kind,target_node_id,target_cluster_id,target_assignment_id,
                  plan_json,observation_json,plan_hash,idempotency_key,expected_policy_revision,
                  expected_assignment_revision,observed_at,target_manifest_json,lifecycle_state,
                  state_revision,requested_by,created_at,expires_at,retention_until
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                plan_id or uuid.uuid4().hex, run_id, operation_kind, target_node_id, target_cluster_id,
                target_assignment_id, canonical_json(redacted_plan), canonical_json(redacted_observation),
                plan_hash, idempotency_key, expected_policy_revision, expected_assignment_revision,
                observed_at, canonical_json(redacted_manifest), state.value, 1, requested_by, created_at,
                iso_timestamp(expires_at), iso_timestamp(retention_until) if retention_until else None,
            ))
        except sqlite3.IntegrityError as error:
            if "maintenance_plans" in str(error) or "UNIQUE constraint" in str(error):
                raise OverlappingPlanError("A conflicting active maintenance plan already exists") from error
            raise
        row = self.connection.execute("SELECT * FROM maintenance_plans WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        return _plan_record(row)

    def get_plan(self, plan_id: str) -> PlanRecord:
        row = self.connection.execute("SELECT * FROM maintenance_plans WHERE id=?", (plan_id,)).fetchone()
        if not row:
            raise RecordNotFound(f"Maintenance plan {plan_id} was not found")
        return _plan_record(row)

    def get_plan_by_idempotency_key(self, idempotency_key: str) -> PlanRecord | None:
        row = self.connection.execute(
            "SELECT * FROM maintenance_plans WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()
        return _plan_record(row) if row else None

    def list_plans(
        self,
        *,
        node_id: int | None = None,
        cluster_id: int | None = None,
        state: str | None = None,
        limit: int = 100,
    ) -> list[PlanRecord]:
        """List redacted maintenance plans through the owned persistence boundary."""
        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        clauses = []
        parameters: list[object] = []
        if node_id is not None:
            clauses.append("target_node_id=?")
            parameters.append(node_id)
        if cluster_id is not None:
            clauses.append("target_cluster_id=?")
            parameters.append(cluster_id)
        if state is not None:
            try:
                MaintenanceState(state)
            except ValueError as error:
                raise ValueError(f"Unsupported maintenance plan state: {state}") from error
            clauses.append("lifecycle_state=?")
            parameters.append(state)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self.connection.execute(
            "SELECT * FROM maintenance_plans" + where + " ORDER BY created_at DESC,id DESC LIMIT ?",
            (*parameters, limit),
        ).fetchall()
        return [_plan_record(row) for row in rows]

    def transition_plan(
        self,
        plan_id: str,
        expected_revision: int,
        target: MaintenanceState | str,
        now: datetime | None = None,
    ) -> PlanRecord:
        current = self.get_plan(plan_id)
        if current.state_revision != expected_revision:
            raise RevisionConflict("Maintenance plan revision changed")
        target_state = MaintenanceState(target)
        validate_plan_transition(current.lifecycle_state, target_state)
        timestamp = iso_timestamp(now)
        approved_at = current.approved_at
        completed_at = current.completed_at
        if target_state == MaintenanceState.EXECUTING and approved_at is None:
            approved_at = timestamp
        if target_state in TERMINAL_PLAN_STATES:
            completed_at = timestamp
        try:
            result = self.connection.execute("""
                UPDATE maintenance_plans
                SET lifecycle_state=?,state_revision=state_revision+1,approved_at=?,completed_at=?
                WHERE id=? AND state_revision=? AND lifecycle_state=?
            """, (target_state.value, approved_at, completed_at, plan_id, expected_revision, current.lifecycle_state.value))
        except sqlite3.IntegrityError as error:
            raise OverlappingPlanError("The target is already covered by another active maintenance plan") from error
        if result.rowcount != 1:
            raise RevisionConflict("Maintenance plan revision changed")
        return self.get_plan(plan_id)

    def verify_plan_hash(self, plan_id: str, expected_hash: str | None = None) -> bool:
        plan = self.get_plan(plan_id)
        if isinstance(plan.plan.get("plan_hash"), str):
            compiled_payload = dict(plan.plan)
            embedded_hash = compiled_payload.pop("plan_hash")
            calculated = canonical_hash(compiled_payload) if embedded_hash == plan.plan_hash else ""
        else:
            calculated = canonical_plan_hash(PlanHashInput(
                operation_kind=plan.operation_kind,
                plan=plan.plan,
                observation=plan.observation,
                target_node_id=plan.target_node_id,
                target_cluster_id=plan.target_cluster_id,
                target_assignment_id=plan.target_assignment_id,
                expected_policy_revision=plan.expected_policy_revision,
                expected_assignment_revision=plan.expected_assignment_revision,
                observed_at=plan.observed_at,
                target_manifest=plan.target_manifest,
            ))
        return calculated == plan.plan_hash and (expected_hash is None or expected_hash == plan.plan_hash)

    def attach_run_id(self, plan_id: str, run_id: int) -> None:
        """Attach a newly created platform run to a plan exactly once."""

        result = self.connection.execute(
            "UPDATE maintenance_plans SET run_id=? WHERE id=? AND run_id IS NULL",
            (run_id, plan_id),
        )
        if result.rowcount != 1:
            raise RevisionConflict("Maintenance run was attached concurrently")

    def list_recovery_plans(self) -> list[PlanRecord]:
        rows = self.connection.execute("""
            SELECT * FROM maintenance_plans
            WHERE lifecycle_state IN ('executing','paused','recovery_required')
            ORDER BY created_at,id
        """).fetchall()
        return [_plan_record(row) for row in rows]

    def prepare_startup_recovery(
        self,
        coordinator: MaintenanceStartupRecoveryCoordinator | None = None,
    ) -> StartupRecovery:
        """Classify interrupted checkpoints before protecting startup artifacts.

        The coordinator consumes only named, read-only projection contracts.
        A checkpoint proven complete closes its plan/run locally; every other
        interrupted operation remains protected and is fail-closed for an
        operator recovery decision.  No host, workload, cluster, or remote
        action is performed here.
        """
        candidates = self.connection.execute("""
            SELECT * FROM maintenance_plans
            WHERE lifecycle_state IN ('executing','paused','recovery_required') OR run_id IS NOT NULL
            ORDER BY created_at,id
        """).fetchall()
        statuses = statuses_in_connection(
            self.connection,
            [row["run_id"] for row in candidates if row["run_id"] is not None],
        )
        rows = [
            row for row in candidates
            if row["lifecycle_state"] in {'executing', 'paused', 'recovery_required'}
            or statuses.get(int(row["run_id"])) in {'queued', 'running', 'recovery_required'}
        ]
        plans = [_plan_record(row) for row in rows]
        classifications = (coordinator or MaintenanceStartupRecoveryCoordinator(self)).classify_plans(plans)
        by_plan_id = {item.plan_id: item for item in classifications}
        protected_run_ids = frozenset(
            int(row["run_id"])
            for row in rows
            if row["run_id"] is not None
            and by_plan_id[row["id"]].classification.value != "complete"
        )
        transitioned_plan_ids = []
        for row in rows:
            state = MaintenanceState(row["lifecycle_state"])
            classification = by_plan_id[row["id"]].classification.value
            if classification == "complete":
                if state == MaintenanceState.PAUSED:
                    resumed = self.transition_plan(row["id"], row["state_revision"], MaintenanceState.EXECUTING)
                    state = resumed.lifecycle_state
                    row = self.connection.execute("SELECT * FROM maintenance_plans WHERE id=?", (row["id"],)).fetchone()
                if state in {MaintenanceState.EXECUTING, MaintenanceState.RECOVERY_REQUIRED}:
                    completed = self.transition_plan(row["id"], row["state_revision"], MaintenanceState.SUCCEEDED)
                    transitioned_plan_ids.append(completed.id)
                if row["run_id"] is not None:
                    finish_run_in_connection(
                        self.connection,
                        int(row["run_id"]),
                        "succeeded",
                        log_suffix="Maintenance checkpoint state was verified during controller startup.\n",
                    )
            elif state in {MaintenanceState.EXECUTING, MaintenanceState.PAUSED}:
                transitioned = self.transition_plan(
                    row["id"], row["state_revision"], MaintenanceState.RECOVERY_REQUIRED,
                )
                transitioned_plan_ids.append(transitioned.id)
        if protected_run_ids:
            mark_recovery_required_in_connection(
                self.connection,
                protected_run_ids,
                "Controller restarted during a maintenance operation; state rediscovery is required.",
            )
        return StartupRecovery(
            protected_run_ids=protected_run_ids,
            discovered_plan_ids=tuple(row["id"] for row in rows),
            transitioned_plan_ids=tuple(transitioned_plan_ids),
            classifications=classifications,
        )

    def observe_conflicts(
        self,
        node_id: int,
        *,
        exclude_plan_id: str | None = None,
        exclude_run_id: int | None = None,
    ) -> ConflictObservation:
        try:
            return MaintenanceReadRepository.from_connection(self.connection).observe_conflicts_in_connection(
                self.connection,
                node_id,
                exclude_plan_id=exclude_plan_id,
                exclude_run_id=exclude_run_id,
            )
        except KeyError as error:
            raise RecordNotFound(f"Node {node_id} was not found") from error

    def list_steps(self, plan_id: str) -> list[StepRecord]:
        rows = self.connection.execute(
            "SELECT * FROM maintenance_steps WHERE plan_id=? ORDER BY sequence", (plan_id,),
        ).fetchall()
        return [_step_record(row) for row in rows]

    def create_step(
        self,
        *,
        plan_id: str,
        step_key: str,
        sequence: int,
        step_kind: str,
        affected_cluster_id: int | None = None,
        affected_assignment_id: int | None = None,
        affected_node_id: int | None = None,
        elasticsearch_node_id: str | None = None,
        before_observation: Mapping[str, Any] | None = None,
    ) -> StepRecord:
        existing = self.connection.execute(
            "SELECT * FROM maintenance_steps WHERE plan_id=? AND step_key=?", (plan_id, step_key),
        ).fetchone()
        expected = {
            "sequence": sequence, "step_kind": step_kind, "affected_cluster_id": affected_cluster_id,
            "affected_assignment_id": affected_assignment_id, "affected_node_id": affected_node_id,
            "elasticsearch_node_id": elasticsearch_node_id, "before_observation_json": _json(before_observation or {}),
        }
        if existing:
            if any(existing[key] != value for key, value in expected.items()):
                raise IdempotencyConflict("Step key was already used for different step content")
            return _step_record(existing)
        now = iso_timestamp()
        try:
            cursor = self.connection.execute("""
                INSERT INTO maintenance_steps(
                  plan_id,step_key,sequence,affected_cluster_id,affected_assignment_id,affected_node_id,
                  elasticsearch_node_id,step_kind,state,state_revision,attempt_count,before_observation_json,
                  after_observation_json,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                plan_id, step_key, sequence, affected_cluster_id, affected_assignment_id, affected_node_id,
                elasticsearch_node_id, step_kind, MaintenanceStepState.PENDING.value, 1, 0,
                expected["before_observation_json"], "{}", now, now,
            ))
        except sqlite3.IntegrityError as error:
            raise IdempotencyConflict("Step sequence or key already exists") from error
        return self.get_step(cursor.lastrowid)

    def get_step(self, step_id: int) -> StepRecord:
        row = self.connection.execute("SELECT * FROM maintenance_steps WHERE id=?", (step_id,)).fetchone()
        if not row:
            raise RecordNotFound(f"Maintenance step {step_id} was not found")
        return _step_record(row)

    def transition_step(
        self,
        step_id: int,
        expected_revision: int,
        target: MaintenanceStepState | str,
        *,
        after_observation: Mapping[str, Any] | None = None,
        error_category: str | None = None,
        resumability_decision: str | None = None,
        now: datetime | None = None,
    ) -> StepRecord:
        current = self.get_step(step_id)
        if current.state_revision != expected_revision:
            raise RevisionConflict("Maintenance step revision changed")
        target_state = MaintenanceStepState(target)
        validate_step_transition(current.state, target_state)
        timestamp = iso_timestamp(now)
        started_at = current.started_at or (timestamp if target_state == MaintenanceStepState.EXECUTING else None)
        finished_at = timestamp if target_state in {
            MaintenanceStepState.VERIFIED, MaintenanceStepState.SKIPPED, MaintenanceStepState.FAILED,
        } else current.finished_at
        attempts = current.attempt_count + (1 if target_state == MaintenanceStepState.EXECUTING else 0)
        result = self.connection.execute("""
            UPDATE maintenance_steps SET state=?,state_revision=state_revision+1,attempt_count=?,
              after_observation_json=?,last_error_category=?,resumability_decision=?,started_at=?,finished_at=?,updated_at=?
            WHERE id=? AND state_revision=? AND state=?
        """, (
            target_state.value, attempts, _json(after_observation or current.after_observation), error_category,
            resumability_decision, started_at, finished_at, timestamp, step_id, expected_revision, current.state.value,
        ))
        if result.rowcount != 1:
            raise RevisionConflict("Maintenance step revision changed")
        return self.get_step(step_id)

    def record_checkpoint(
        self,
        *,
        plan_id: str,
        checkpoint_key: str,
        sequence: int,
        side_effect_state: SideEffectState | str,
        payload: Mapping[str, Any],
        step_id: int | None = None,
        observation: Mapping[str, Any] | None = None,
    ) -> CheckpointRecord:
        state = SideEffectState(side_effect_state)
        payload_json = _json(payload)
        observation_json = _json(observation or {})
        existing = self.connection.execute(
            "SELECT * FROM maintenance_checkpoints WHERE plan_id=? AND checkpoint_key=?", (plan_id, checkpoint_key),
        ).fetchone()
        if existing:
            expected = (step_id, sequence, state.value, payload_json, observation_json)
            actual = (existing["step_id"], existing["sequence"], existing["side_effect_state"], existing["payload_json"], existing["observation_json"])
            if actual != expected:
                raise IdempotencyConflict("Checkpoint key was already used for different checkpoint content")
            return _checkpoint_record(existing)
        try:
            cursor = self.connection.execute("""
                INSERT INTO maintenance_checkpoints(
                  plan_id,step_id,checkpoint_key,sequence,side_effect_state,payload_json,observation_json,created_at
                ) VALUES(?,?,?,?,?,?,?,?)
            """, (plan_id, step_id, checkpoint_key, sequence, state.value, payload_json, observation_json, iso_timestamp()))
        except sqlite3.IntegrityError as error:
            raise IdempotencyConflict("Checkpoint sequence or key already exists") from error
        return self.get_checkpoint(cursor.lastrowid)

    def get_checkpoint(self, checkpoint_id: int) -> CheckpointRecord:
        row = self.connection.execute("SELECT * FROM maintenance_checkpoints WHERE id=?", (checkpoint_id,)).fetchone()
        if not row:
            raise RecordNotFound(f"Maintenance checkpoint {checkpoint_id} was not found")
        return _checkpoint_record(row)

    def list_checkpoints(self, plan_id: str) -> list[CheckpointRecord]:
        rows = self.connection.execute(
            "SELECT * FROM maintenance_checkpoints WHERE plan_id=? ORDER BY sequence", (plan_id,),
        ).fetchall()
        return [_checkpoint_record(row) for row in rows]

    def latest_checkpoint(self, plan_id: str) -> CheckpointRecord | None:
        row = self.connection.execute(
            "SELECT * FROM maintenance_checkpoints WHERE plan_id=? ORDER BY sequence DESC LIMIT 1", (plan_id,),
        ).fetchone()
        return _checkpoint_record(row) if row else None

    def classify_checkpoint(
        self,
        checkpoint_id: int,
        evidence: RecoveryEvidence,
        expected_revision: int,
        now: datetime | None = None,
    ) -> tuple[CheckpointRecord, RecoveryDecision]:
        checkpoint = self.get_checkpoint(checkpoint_id)
        if checkpoint.classification_revision != expected_revision:
            raise RevisionConflict("Maintenance checkpoint classification revision changed")
        if SideEffectState(evidence.side_effect_state) != checkpoint.side_effect_state:
            raise ValueError("Recovery evidence side-effect state does not match the checkpoint")
        decision = classify_recovery(evidence)
        evidence_json = _json({
            "observation_complete": evidence.observation_complete,
            "observed_fingerprint": evidence.observed_fingerprint,
            "before_fingerprint": evidence.before_fingerprint,
            "after_fingerprint": evidence.after_fingerprint,
            "identity_matches": evidence.identity_matches,
            "resume_is_idempotent": evidence.resume_is_idempotent,
        })
        result = self.connection.execute("""
            UPDATE maintenance_checkpoints SET recovery_evidence_json=?,recovery_classification=?,
              recovery_reason_code=?,resumable=?,classification_revision=classification_revision+1,classified_at=?
            WHERE id=? AND classification_revision=?
        """, (
            evidence_json, decision.classification.value, decision.reason_code, int(decision.resumable),
            iso_timestamp(now), checkpoint_id, expected_revision,
        ))
        if result.rowcount != 1:
            raise RevisionConflict("Maintenance checkpoint classification revision changed")
        return self.get_checkpoint(checkpoint_id), decision

    def persist_startup_classification(
        self,
        checkpoint_id: int,
        *,
        expected_revision: int,
        classification: str,
        reason_code: str,
        resumable: bool,
        evidence: Mapping[str, Any],
        now: datetime | None = None,
    ) -> CheckpointRecord:
        """Persist redacted startup-only classification through maintenance ownership.

        This is deliberately separate from ``classify_checkpoint`` because
        startup adds the operator-facing ``recovery_required`` state while the
        legacy recovery API retains its ``safe_to_resume`` compatibility enum.
        """

        allowed = {"complete", "incomplete", "ambiguous", "recovery_required"}
        if classification not in allowed:
            raise ValueError("Unsupported startup recovery classification")
        checkpoint = self.get_checkpoint(checkpoint_id)
        if checkpoint.classification_revision != expected_revision:
            raise RevisionConflict("Maintenance checkpoint classification revision changed")
        persisted_evidence = dict(checkpoint.recovery_evidence)
        persisted_evidence["startup_reason_code"] = reason_code
        persisted_evidence["startup"] = dict(evidence)
        result = self.connection.execute("""
            UPDATE maintenance_checkpoints SET recovery_evidence_json=?,recovery_classification=?,
              recovery_reason_code=?,resumable=?,classification_revision=classification_revision+1,classified_at=?
            WHERE id=? AND classification_revision=?
        """, (
            _json(persisted_evidence), classification, reason_code, int(resumable),
            iso_timestamp(now), checkpoint_id, expected_revision,
        ))
        if result.rowcount != 1:
            raise RevisionConflict("Maintenance checkpoint classification revision changed")
        return self.get_checkpoint(checkpoint_id)

    def find_host_state(self, node_id: int) -> HostStateRecord | None:
        row = self.connection.execute("SELECT * FROM host_maintenance_state WHERE node_id=?", (node_id,)).fetchone()
        return _host_record(row) if row else None

    def get_host_state(self, node_id: int) -> HostStateRecord:
        row = self.connection.execute("SELECT * FROM host_maintenance_state WHERE node_id=?", (node_id,)).fetchone()
        if not row:
            now = iso_timestamp()
            self.connection.execute(
                "INSERT INTO host_maintenance_state(node_id,state,active_plan_id,state_revision,entered_at,updated_at) VALUES(?,?,?,?,?,?)",
                (node_id, HostMaintenanceState.AVAILABLE.value, None, 1, now, now),
            )
            row = self.connection.execute("SELECT * FROM host_maintenance_state WHERE node_id=?", (node_id,)).fetchone()
        return _host_record(row)

    def transition_host_state(
        self,
        node_id: int,
        expected_revision: int,
        target: HostMaintenanceState | str,
        active_plan_id: str | None,
        now: datetime | None = None,
    ) -> HostStateRecord:
        current = self.get_host_state(node_id)
        if current.state_revision != expected_revision:
            raise RevisionConflict("Host maintenance state revision changed")
        target_state = HostMaintenanceState(target)
        validate_host_transition(current.state, target_state)
        if target_state == HostMaintenanceState.AVAILABLE:
            active_plan_id = None
        elif not active_plan_id:
            raise ValueError("An active plan is required outside the available host state")
        timestamp = iso_timestamp(now)
        result = self.connection.execute("""
            UPDATE host_maintenance_state SET state=?,active_plan_id=?,state_revision=state_revision+1,
              entered_at=?,updated_at=? WHERE node_id=? AND state_revision=? AND state=?
        """, (target_state.value, active_plan_id, timestamp, timestamp, node_id, expected_revision, current.state.value))
        if result.rowcount != 1:
            raise RevisionConflict("Host maintenance state revision changed")
        return self.get_host_state(node_id)

    def acquire_locks(
        self,
        requests: Sequence[LockRequest],
        *,
        owner_plan_id: str,
        run_id: int | None = None,
        ttl_seconds: int = 300,
        owner_token: str | None = None,
        now: datetime | None = None,
    ) -> list[LockRecord]:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        scopes = sorted({request.normalized() for request in requests})
        if not scopes:
            raise ValueError("At least one lock scope is required")
        token = owner_token or secrets.token_urlsafe(32)
        current_time = now or utc_now()
        acquired_at = iso_timestamp(current_time)
        expires_at = iso_timestamp(current_time + timedelta(seconds=ttl_seconds))
        self.connection.execute("SAVEPOINT maintenance_lock_acquire")
        try:
            records: list[LockRecord] = []
            for scope, identifier in scopes:
                existing_row = self.connection.execute(
                    "SELECT * FROM maintenance_locks WHERE scope_kind=? AND scope_id=? AND released_at IS NULL",
                    (scope, identifier),
                ).fetchone()
                if existing_row:
                    existing = _lock_record(existing_row)
                    if existing.expired(current_time):
                        raise StaleLockRequiresRecovery(f"Expired {scope} lock {identifier} requires rediscovery before release")
                    if existing.owner_plan_id == owner_plan_id and existing.owner_token == token:
                        records.append(existing)
                        continue
                    raise LockConflict(f"Active {scope} lock already covers {identifier}")
                cursor = self.connection.execute("""
                    INSERT INTO maintenance_locks(
                      scope_kind,scope_id,owner_plan_id,run_id,owner_token,acquired_at,heartbeat_at,expires_at
                    ) VALUES(?,?,?,?,?,?,?,?)
                """, (scope, identifier, owner_plan_id, run_id, token, acquired_at, acquired_at, expires_at))
                records.append(self.get_lock(cursor.lastrowid))
            self.connection.execute("RELEASE SAVEPOINT maintenance_lock_acquire")
            return records
        except sqlite3.IntegrityError as error:
            self.connection.execute("ROLLBACK TO SAVEPOINT maintenance_lock_acquire")
            self.connection.execute("RELEASE SAVEPOINT maintenance_lock_acquire")
            raise LockConflict("A maintenance lock was acquired concurrently") from error
        except Exception:
            self.connection.execute("ROLLBACK TO SAVEPOINT maintenance_lock_acquire")
            self.connection.execute("RELEASE SAVEPOINT maintenance_lock_acquire")
            raise

    def get_lock(self, lock_id: int) -> LockRecord:
        row = self.connection.execute("SELECT * FROM maintenance_locks WHERE id=?", (lock_id,)).fetchone()
        if not row:
            raise RecordNotFound(f"Maintenance lock {lock_id} was not found")
        return _lock_record(row)

    def list_active_locks(self, owner_plan_id: str | None = None) -> list[LockRecord]:
        if owner_plan_id:
            rows = self.connection.execute(
                "SELECT * FROM maintenance_locks WHERE released_at IS NULL AND owner_plan_id=? ORDER BY scope_kind,scope_id",
                (owner_plan_id,),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM maintenance_locks WHERE released_at IS NULL ORDER BY scope_kind,scope_id"
            ).fetchall()
        return [_lock_record(row) for row in rows]

    def list_stale_locks(self, now: datetime | None = None) -> list[LockRecord]:
        timestamp = iso_timestamp(now)
        rows = self.connection.execute("""
            SELECT * FROM maintenance_locks
            WHERE released_at IS NULL AND expires_at<=?
            ORDER BY expires_at,scope_kind,scope_id
        """, (timestamp,)).fetchall()
        return [_lock_record(row) for row in rows]

    def heartbeat_locks(
        self,
        owner_token: str,
        ttl_seconds: int = 300,
        now: datetime | None = None,
    ) -> list[LockRecord]:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        current_time = now or utc_now()
        locks = [lock for lock in self.list_active_locks() if lock.owner_token == owner_token]
        if not locks:
            raise LockOwnershipError("No active maintenance locks belong to the owner token")
        if any(lock.expired(current_time) for lock in locks):
            raise StaleLockRequiresRecovery("An expired maintenance lock requires rediscovery before heartbeat")
        heartbeat_at = iso_timestamp(current_time)
        expires_at = iso_timestamp(current_time + timedelta(seconds=ttl_seconds))
        self.connection.execute(
            "UPDATE maintenance_locks SET heartbeat_at=?,expires_at=? WHERE owner_token=? AND released_at IS NULL",
            (heartbeat_at, expires_at, owner_token),
        )
        return [lock for lock in self.list_active_locks() if lock.owner_token == owner_token]

    def release_locks(
        self,
        owner_token: str,
        *,
        reason: str = "completed",
        observation: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> int:
        current_time = now or utc_now()
        locks = [lock for lock in self.list_active_locks() if lock.owner_token == owner_token]
        if not locks:
            raise LockOwnershipError("No active maintenance locks belong to the owner token")
        if any(lock.expired(current_time) for lock in locks):
            raise StaleLockRequiresRecovery("An expired maintenance lock requires rediscovery before release")
        released_at = iso_timestamp(current_time)
        result = self.connection.execute("""
            UPDATE maintenance_locks SET released_at=?,release_reason=?,release_observation_json=?
            WHERE owner_token=? AND released_at IS NULL
        """, (released_at, reason, _json(observation or {}), owner_token))
        return result.rowcount

    def recover_stale_lock(
        self,
        lock_id: int,
        *,
        observation: Mapping[str, Any],
        recovered_by: str,
        reason: str,
        now: datetime | None = None,
    ) -> LockRecord:
        if not observation:
            raise ValueError("Stale lock release requires rediscovery evidence")
        lock = self.get_lock(lock_id)
        current_time = now or utc_now()
        if lock.released_at:
            raise LockOwnershipError("Maintenance lock is already released")
        if not lock.expired(current_time):
            raise LockConflict("A non-expired maintenance lock cannot be recovered")
        timestamp = iso_timestamp(current_time)
        result = self.connection.execute("""
            UPDATE maintenance_locks SET released_at=?,stale_released_at=?,release_reason=?,
              release_observation_json=?,recovered_by=? WHERE id=? AND released_at IS NULL
        """, (timestamp, timestamp, reason, _json(observation), recovered_by, lock_id))
        if result.rowcount != 1:
            raise LockOwnershipError("Maintenance lock was released concurrently")
        return self.get_lock(lock_id)

    def record_audit(
        self,
        *,
        username: str,
        action: str,
        detail: Mapping[str, Any],
        cluster_id: int | None = None,
        item_id: str = "",
    ) -> int:
        return write_event_in_connection(
            self.connection,
            username,
            action,
            cluster_id=cluster_id,
            item_id=item_id,
            detail=_json(detail),
        )

    def prune_completed_plans(self, retention_before: datetime) -> int:
        threshold = iso_timestamp(retention_before)
        result = self.connection.execute("""
            DELETE FROM maintenance_plans
            WHERE lifecycle_state IN ('succeeded','failed','cancelled')
              AND retention_until IS NOT NULL AND retention_until < ?
              AND NOT EXISTS (
                SELECT 1 FROM maintenance_locks
                WHERE maintenance_locks.owner_plan_id=maintenance_plans.id
                  AND maintenance_locks.released_at IS NULL
              )
        """, (threshold,))
        return result.rowcount
