"""Certificate-owned SQLite persistence for lifecycle metadata only."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Mapping
from uuid import NAMESPACE_URL, uuid4, uuid5

from app.modules.platform import MigrationRegistry

from .contracts import DEFAULT_CERTIFICATE_POLICY, CertificateNotFound, CertificateRevisionConflict


SCHEMA_VERSION = 1
SCHEMA_NAME = "certificate_lifecycle_foundation"
SCHEMA_CHECKSUM = sha256(f"{SCHEMA_VERSION}:{SCHEMA_NAME}".encode()).hexdigest()
SCHEMA_TABLES = (
    "certificate_trust_domains",
    "certificate_policies",
    "certificate_assets",
    "certificate_generations",
    "certificate_observations",
    "certificate_trust_consumers",
    "certificate_deployments",
    "certificate_operations",
    "certificate_operation_steps",
)


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _json(value: Mapping[str, Any] | list[Any] | tuple[Any, ...] | None = None) -> str:
    return json.dumps(value or {}, sort_keys=True, separators=(",", ":"))


def _load_json(value: str | None, fallback: Any) -> Any:
    try:
        result = json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return fallback
    return result if isinstance(result, type(fallback)) else fallback


def _id(namespace: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"elkeeper:{namespace}"))


def _create_schema(connection: sqlite3.Connection) -> None:
    schema = """
        CREATE TABLE IF NOT EXISTS certificate_trust_domains (
          id TEXT PRIMARY KEY,
          cluster_id INTEGER NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
          kind TEXT NOT NULL,
          state TEXT NOT NULL DEFAULT 'active',
          legacy_shared INTEGER NOT NULL DEFAULT 0,
          split_migration_state TEXT NOT NULL DEFAULT 'not_required',
          compatibility_profile TEXT NOT NULL DEFAULT '',
          verification_mode TEXT NOT NULL DEFAULT 'certificate',
          revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(cluster_id, kind)
        );
        CREATE TABLE IF NOT EXISTS certificate_policies (
          id TEXT PRIMARY KEY,
          cluster_id INTEGER NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
          trust_domain_id TEXT REFERENCES certificate_trust_domains(id) ON DELETE CASCADE,
          revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
          policy_json TEXT NOT NULL DEFAULT '{}',
          updated_by TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS certificate_policies_default_cluster
          ON certificate_policies(cluster_id) WHERE trust_domain_id IS NULL;
        CREATE UNIQUE INDEX IF NOT EXISTS certificate_policies_domain
          ON certificate_policies(cluster_id, trust_domain_id) WHERE trust_domain_id IS NOT NULL;
        CREATE TABLE IF NOT EXISTS certificate_assets (
          id TEXT PRIMARY KEY,
          cluster_id INTEGER NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
          trust_domain_id TEXT NOT NULL REFERENCES certificate_trust_domains(id) ON DELETE CASCADE,
          owner_type TEXT NOT NULL,
          owner_id TEXT NOT NULL,
          purpose TEXT NOT NULL,
          provider_type TEXT NOT NULL,
          management_state TEXT NOT NULL,
          storage_locator_json TEXT NOT NULL DEFAULT '{}',
          desired_identity_json TEXT NOT NULL DEFAULT '{}',
          active_generation_id TEXT,
          health_state TEXT NOT NULL DEFAULT 'unobserved',
          first_observed_at TEXT,
          last_observed_at TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(trust_domain_id, owner_type, owner_id, purpose)
        );
        CREATE TABLE IF NOT EXISTS certificate_generations (
          id TEXT PRIMARY KEY,
          asset_id TEXT NOT NULL REFERENCES certificate_assets(id) ON DELETE CASCADE,
          certificate_fingerprint TEXT NOT NULL DEFAULT '',
          public_metadata_json TEXT NOT NULL DEFAULT '{}',
          chain_fingerprints_json TEXT NOT NULL DEFAULT '[]',
          issuer_generation_id TEXT,
          source TEXT NOT NULL,
          state TEXT NOT NULL,
          certificate_format TEXT NOT NULL DEFAULT 'PEM',
          public_certificate_locator_json TEXT NOT NULL DEFAULT '{}',
          trust_bundle_locator_json TEXT NOT NULL DEFAULT '{}',
          encrypted_secret_ref TEXT,
          created_at TEXT NOT NULL,
          staged_at TEXT,
          activated_at TEXT,
          verified_at TEXT,
          retired_at TEXT,
          UNIQUE(asset_id, certificate_fingerprint)
        );
        CREATE TABLE IF NOT EXISTS certificate_observations (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          asset_id TEXT NOT NULL REFERENCES certificate_assets(id) ON DELETE CASCADE,
          generation_fingerprint TEXT NOT NULL DEFAULT '',
          metadata_json TEXT NOT NULL DEFAULT '{}',
          validation_json TEXT NOT NULL DEFAULT '{}',
          endpoint_json TEXT NOT NULL DEFAULT '{}',
          source TEXT NOT NULL,
          error_code TEXT,
          error_message TEXT,
          observed_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS certificate_observations_asset_time
          ON certificate_observations(asset_id, observed_at DESC);
        CREATE TABLE IF NOT EXISTS certificate_trust_consumers (
          id TEXT PRIMARY KEY,
          cluster_id INTEGER NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
          trust_domain_id TEXT NOT NULL REFERENCES certificate_trust_domains(id) ON DELETE CASCADE,
          consumer_type TEXT NOT NULL,
          consumer_kind TEXT NOT NULL,
          owner_id TEXT,
          description TEXT NOT NULL DEFAULT '',
          verification_method TEXT NOT NULL,
          trust_state TEXT NOT NULL DEFAULT 'unknown',
          candidate_trust_state TEXT NOT NULL DEFAULT 'unknown',
          last_verified_at TEXT,
          attestation_expires_at TEXT,
          revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
          blocking_reason TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(cluster_id, trust_domain_id, consumer_type, consumer_kind, owner_id)
        );
        CREATE TABLE IF NOT EXISTS certificate_deployments (
          id TEXT PRIMARY KEY,
          operation_id TEXT,
          asset_id TEXT NOT NULL REFERENCES certificate_assets(id) ON DELETE CASCADE,
          generation_id TEXT REFERENCES certificate_generations(id) ON DELETE SET NULL,
          target_owner_id TEXT NOT NULL,
          deployment_kind TEXT NOT NULL,
          desired_fingerprint TEXT NOT NULL DEFAULT '',
          state TEXT NOT NULL,
          result_code TEXT,
          staged_at TEXT,
          activated_at TEXT,
          verified_at TEXT,
          restored_at TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS certificate_operations (
          id TEXT PRIMARY KEY,
          cluster_id INTEGER NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
          operation_type TEXT NOT NULL,
          state TEXT NOT NULL,
          revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
          trust_domain_ids_json TEXT NOT NULL DEFAULT '[]',
          request_hash TEXT NOT NULL,
          policy_revision INTEGER,
          run_id INTEGER,
          maintenance_plan_id TEXT,
          phase TEXT NOT NULL,
          blockers_json TEXT NOT NULL DEFAULT '[]',
          summary_json TEXT NOT NULL DEFAULT '{}',
          requested_by TEXT NOT NULL,
          approved_by TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          completed_at TEXT
        );
        CREATE INDEX IF NOT EXISTS certificate_operations_cluster_time
          ON certificate_operations(cluster_id, created_at DESC);
        CREATE TABLE IF NOT EXISTS certificate_operation_steps (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          operation_id TEXT NOT NULL REFERENCES certificate_operations(id) ON DELETE CASCADE,
          step_key TEXT NOT NULL,
          sequence INTEGER NOT NULL CHECK(sequence >= 0),
          phase TEXT NOT NULL,
          target_id TEXT,
          command_type TEXT NOT NULL,
          idempotency_key TEXT NOT NULL,
          state TEXT NOT NULL,
          result_json TEXT NOT NULL DEFAULT '{}',
          attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count >= 0),
          created_at TEXT NOT NULL,
          started_at TEXT,
          finished_at TEXT,
          verified_at TEXT,
          UNIQUE(operation_id, step_key),
          UNIQUE(operation_id, sequence)
        );
        """
    for statement in schema.split(";"):
        if statement.strip():
            connection.execute(statement)


def install_certificate_schema(connection: sqlite3.Connection) -> int:
    """Install certificate-owned additive schema without touching remote TLS."""

    connection.execute("SAVEPOINT certificate_schema_install")
    try:
        registry = MigrationRegistry(connection, "certificate_schema_migrations")
        registry.ensure()
        applied = registry.applied()
        if set(applied) - {SCHEMA_VERSION}:
            unknown = ", ".join(str(item) for item in sorted(set(applied) - {SCHEMA_VERSION}))
            raise ValueError(f"Unknown certificate migration versions: {unknown}")
        existing = applied.get(SCHEMA_VERSION)
        if existing and (existing["name"], existing["checksum"]) != (SCHEMA_NAME, SCHEMA_CHECKSUM):
            raise ValueError("Certificate migration checksum does not match")
        _create_schema(connection)
        if not existing:
            registry.record(SCHEMA_VERSION, SCHEMA_NAME, SCHEMA_CHECKSUM, utc_timestamp())
        connection.execute("RELEASE SAVEPOINT certificate_schema_install")
        return SCHEMA_VERSION
    except Exception:
        connection.execute("ROLLBACK TO SAVEPOINT certificate_schema_install")
        connection.execute("RELEASE SAVEPOINT certificate_schema_install")
        raise


class CertificateRepository:
    """Persistence boundary for certificate lifecycle metadata and projections."""

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    @staticmethod
    def schema_tables() -> tuple[str, ...]:
        return SCHEMA_TABLES

    def ensure_domain(
        self,
        *,
        cluster_id: int,
        kind: str,
        compatibility_profile: str,
        legacy_shared: bool = True,
    ) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM certificate_trust_domains WHERE cluster_id=? AND kind=?",
            (cluster_id, kind),
        ).fetchone()
        timestamp = utc_timestamp()
        if row is None:
            domain_id = _id(f"trust-domain:{cluster_id}:{kind}")
            self.connection.execute(
                "INSERT INTO certificate_trust_domains("
                "id,cluster_id,kind,state,legacy_shared,split_migration_state,compatibility_profile,"
                "verification_mode,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    domain_id,
                    cluster_id,
                    kind,
                    "active",
                    int(legacy_shared),
                    "legacy_shared_detected" if legacy_shared else "not_required",
                    compatibility_profile,
                    "certificate" if kind == "elasticsearch_transport" else "full",
                    timestamp,
                    timestamp,
                ),
            )
            row = self.connection.execute(
                "SELECT * FROM certificate_trust_domains WHERE id=?", (domain_id,)
            ).fetchone()
        elif row["compatibility_profile"] != compatibility_profile:
            self.connection.execute(
                "UPDATE certificate_trust_domains SET compatibility_profile=?,updated_at=?,revision=revision+1 WHERE id=?",
                (compatibility_profile, timestamp, row["id"]),
            )
            row = self.connection.execute(
                "SELECT * FROM certificate_trust_domains WHERE id=?", (row["id"],)
            ).fetchone()
        return self._domain(row)

    def list_domains(self, cluster_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM certificate_trust_domains WHERE cluster_id=? ORDER BY kind", (cluster_id,)
        ).fetchall()
        return [self._domain(row) for row in rows]

    def get_domain(self, domain_id: str, *, cluster_id: int) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM certificate_trust_domains WHERE id=? AND cluster_id=?",
            (domain_id, cluster_id),
        ).fetchone()
        if row is None:
            raise CertificateNotFound("Certificate trust domain not found")
        return self._domain(row)

    def ensure_default_policy(self, cluster_id: int, *, username: str = "system") -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM certificate_policies WHERE cluster_id=? AND trust_domain_id IS NULL", (cluster_id,)
        ).fetchone()
        if row is None:
            timestamp = utc_timestamp()
            policy_id = _id(f"certificate-policy:{cluster_id}:default")
            self.connection.execute(
                "INSERT INTO certificate_policies(id,cluster_id,trust_domain_id,revision,policy_json,updated_by,created_at,updated_at) "
                "VALUES(?,?,NULL,1,?,?,?,?)",
                (policy_id, cluster_id, _json(DEFAULT_CERTIFICATE_POLICY), username, timestamp, timestamp),
            )
            row = self.connection.execute("SELECT * FROM certificate_policies WHERE id=?", (policy_id,)).fetchone()
        return self._policy(row)

    def update_default_policy(
        self,
        cluster_id: int,
        *,
        policy: Mapping[str, object],
        expected_revision: int,
        username: str,
    ) -> dict[str, Any]:
        current = self.ensure_default_policy(cluster_id)
        if current["revision"] != expected_revision:
            raise CertificateRevisionConflict("Certificate policy changed; refresh before saving")
        timestamp = utc_timestamp()
        cursor = self.connection.execute(
            "UPDATE certificate_policies SET policy_json=?,revision=revision+1,updated_by=?,updated_at=? "
            "WHERE id=? AND revision=?",
            (_json(dict(policy)), username, timestamp, current["id"], expected_revision),
        )
        if cursor.rowcount != 1:
            raise CertificateRevisionConflict("Certificate policy changed; refresh before saving")
        row = self.connection.execute("SELECT * FROM certificate_policies WHERE id=?", (current["id"],)).fetchone()
        return self._policy(row)

    def ensure_asset(
        self,
        *,
        cluster_id: int,
        trust_domain_id: str,
        owner_type: str,
        owner_id: str,
        purpose: str,
        storage_locator: Mapping[str, object],
        desired_identity: Mapping[str, object],
        provider_type: str = "managed_legacy",
    ) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM certificate_assets WHERE trust_domain_id=? AND owner_type=? AND owner_id=? AND purpose=?",
            (trust_domain_id, owner_type, owner_id, purpose),
        ).fetchone()
        timestamp = utc_timestamp()
        if row is None:
            asset_id = _id(f"asset:{trust_domain_id}:{owner_type}:{owner_id}:{purpose}")
            self.connection.execute(
                "INSERT INTO certificate_assets("
                "id,cluster_id,trust_domain_id,owner_type,owner_id,purpose,provider_type,management_state,"
                "storage_locator_json,desired_identity_json,health_state,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    asset_id,
                    cluster_id,
                    trust_domain_id,
                    owner_type,
                    owner_id,
                    purpose,
                    provider_type,
                    "observed",
                    _json(dict(storage_locator)),
                    _json(dict(desired_identity)),
                    "unobserved",
                    timestamp,
                    timestamp,
                ),
            )
            row = self.connection.execute("SELECT * FROM certificate_assets WHERE id=?", (asset_id,)).fetchone()
        return self._asset(row)

    def list_assets(self, cluster_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT a.*,d.kind AS trust_domain_kind,d.legacy_shared,d.split_migration_state "
            "FROM certificate_assets a JOIN certificate_trust_domains d ON d.id=a.trust_domain_id "
            "WHERE a.cluster_id=? ORDER BY a.owner_type,a.owner_id,a.purpose",
            (cluster_id,),
        ).fetchall()
        return [self._asset(row, include_domain=True) for row in rows]

    def get_asset(self, asset_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT a.*,d.kind AS trust_domain_kind,d.legacy_shared,d.split_migration_state "
            "FROM certificate_assets a JOIN certificate_trust_domains d ON d.id=a.trust_domain_id WHERE a.id=?",
            (asset_id,),
        ).fetchone()
        if row is None:
            raise CertificateNotFound("Certificate asset not found")
        return self._asset(row, include_domain=True)

    def record_observation(
        self,
        asset_id: str,
        *,
        metadata: Mapping[str, object],
        validation: Mapping[str, object],
        chain_fingerprints: tuple[str, ...],
        source: str,
        endpoint: Mapping[str, object] | None = None,
    ) -> dict[str, Any]:
        """Persist public certificate evidence while keeping PEM transient."""

        asset = self.get_asset(asset_id)
        fingerprint = str(metadata.get("fingerprint", ""))
        if not fingerprint:
            raise ValueError("Certificate observation requires a fingerprint")
        timestamp = utc_timestamp()
        generation = self.connection.execute(
            "SELECT * FROM certificate_generations WHERE asset_id=? AND certificate_fingerprint=?",
            (asset_id, fingerprint),
        ).fetchone()
        if generation is None:
            generation_id = str(uuid4())
            self.connection.execute(
                "INSERT INTO certificate_generations("
                "id,asset_id,certificate_fingerprint,public_metadata_json,chain_fingerprints_json,source,state,"
                "certificate_format,created_at,verified_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    generation_id,
                    asset_id,
                    fingerprint,
                    _json(dict(metadata)),
                    _json(list(chain_fingerprints)),
                    source,
                    "observed",
                    "PEM",
                    timestamp,
                    timestamp,
                ),
            )
            generation = self.connection.execute(
                "SELECT * FROM certificate_generations WHERE id=?", (generation_id,)
            ).fetchone()
        observation_cursor = self.connection.execute(
            "INSERT INTO certificate_observations("
            "asset_id,generation_fingerprint,metadata_json,validation_json,endpoint_json,source,observed_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                asset_id,
                fingerprint,
                _json(dict(metadata)),
                _json(dict(validation)),
                _json(dict(endpoint or {})),
                source,
                timestamp,
            ),
        )
        health = str(validation.get("health", "degraded"))
        self.connection.execute(
            "UPDATE certificate_assets SET active_generation_id=COALESCE(active_generation_id,?),health_state=?,"
            "first_observed_at=COALESCE(first_observed_at,?),last_observed_at=?,updated_at=? WHERE id=?",
            (generation["id"], health, timestamp, timestamp, timestamp, asset_id),
        )
        observation = self.connection.execute(
            "SELECT * FROM certificate_observations WHERE id=?", (observation_cursor.lastrowid,)
        ).fetchone()
        return {
            "asset": self.get_asset(asset_id),
            "generation": self._generation(generation),
            "observation": self._observation(observation),
        }

    def list_generations(self, asset_id: str) -> list[dict[str, Any]]:
        self.get_asset(asset_id)
        rows = self.connection.execute(
            "SELECT * FROM certificate_generations WHERE asset_id=? ORDER BY created_at DESC,id DESC", (asset_id,)
        ).fetchall()
        return [self._generation(row) for row in rows]

    def list_observations(self, asset_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        self.get_asset(asset_id)
        rows = self.connection.execute(
            "SELECT * FROM certificate_observations WHERE asset_id=? ORDER BY observed_at DESC,id DESC LIMIT ?",
            (asset_id, max(1, min(int(limit), 200))),
        ).fetchall()
        return [self._observation(row) for row in rows]

    def record_collection_failure(self, asset_id: str, *, source: str, error_code: str) -> dict[str, Any]:
        """Persist a generic collection failure without retaining remote output."""

        self.get_asset(asset_id)
        timestamp = utc_timestamp()
        cursor = self.connection.execute(
            "INSERT INTO certificate_observations("
            "asset_id,metadata_json,validation_json,endpoint_json,source,error_code,observed_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                asset_id,
                _json({}),
                _json({"health": "observation_failed", "collection": "failed"}),
                _json({}),
                source,
                error_code,
                timestamp,
            ),
        )
        self.connection.execute(
            "UPDATE certificate_assets SET health_state='observation_failed',"
            "first_observed_at=COALESCE(first_observed_at,?),last_observed_at=?,updated_at=? WHERE id=?",
            (timestamp, timestamp, timestamp, asset_id),
        )
        observation = self.connection.execute(
            "SELECT * FROM certificate_observations WHERE id=?", (cursor.lastrowid,)
        ).fetchone()
        return {"asset": self.get_asset(asset_id), "observation": self._observation(observation)}

    def ensure_managed_consumer(
        self,
        *,
        cluster_id: int,
        trust_domain_id: str,
        consumer_kind: str,
        owner_id: str,
        description: str,
    ) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM certificate_trust_consumers WHERE cluster_id=? AND trust_domain_id=? "
            "AND consumer_type='managed' AND consumer_kind=? AND owner_id=?",
            (cluster_id, trust_domain_id, consumer_kind, owner_id),
        ).fetchone()
        timestamp = utc_timestamp()
        if row is None:
            consumer_id = _id(f"consumer:{cluster_id}:{trust_domain_id}:managed:{consumer_kind}:{owner_id}")
            self.connection.execute(
                "INSERT INTO certificate_trust_consumers("
                "id,cluster_id,trust_domain_id,consumer_type,consumer_kind,owner_id,description,verification_method,"
                "trust_state,candidate_trust_state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    consumer_id,
                    cluster_id,
                    trust_domain_id,
                    "managed",
                    consumer_kind,
                    owner_id,
                    description,
                    "managed_probe",
                    "unknown",
                    "unknown",
                    timestamp,
                    timestamp,
                ),
            )
            row = self.connection.execute("SELECT * FROM certificate_trust_consumers WHERE id=?", (consumer_id,)).fetchone()
        return self._consumer(row)

    def list_consumers(self, cluster_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT c.*,d.kind AS trust_domain_kind FROM certificate_trust_consumers c "
            "JOIN certificate_trust_domains d ON d.id=c.trust_domain_id "
            "WHERE c.cluster_id=? ORDER BY d.kind,c.consumer_type,c.consumer_kind,c.owner_id",
            (cluster_id,),
        ).fetchall()
        return [self._consumer(row, include_domain=True) for row in rows]

    def declare_external_consumer(
        self,
        *,
        cluster_id: int,
        trust_domain_id: str,
        consumer_kind: str,
        description: str,
        verification_method: str,
    ) -> dict[str, Any]:
        self.get_domain(trust_domain_id, cluster_id=cluster_id)
        owner_id = _id(f"external-consumer:{cluster_id}:{trust_domain_id}:{consumer_kind}:{description}")
        row = self.connection.execute(
            "SELECT * FROM certificate_trust_consumers WHERE cluster_id=? AND trust_domain_id=? "
            "AND consumer_type='external' AND consumer_kind=? AND owner_id=?",
            (cluster_id, trust_domain_id, consumer_kind, owner_id),
        ).fetchone()
        timestamp = utc_timestamp()
        if row is None:
            consumer_id = str(uuid4())
            self.connection.execute(
                "INSERT INTO certificate_trust_consumers("
                "id,cluster_id,trust_domain_id,consumer_type,consumer_kind,owner_id,description,verification_method,"
                "trust_state,candidate_trust_state,blocking_reason,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    consumer_id,
                    cluster_id,
                    trust_domain_id,
                    "external",
                    consumer_kind,
                    owner_id,
                    description,
                    verification_method,
                    "unverified",
                    "unverified",
                    "external_consumer_unverified",
                    timestamp,
                    timestamp,
                ),
            )
            row = self.connection.execute("SELECT * FROM certificate_trust_consumers WHERE id=?", (consumer_id,)).fetchone()
        return self._consumer(row)

    def create_preview_operation(
        self,
        *,
        cluster_id: int,
        operation_type: str,
        trust_domain_ids: tuple[str, ...],
        request_hash: str,
        policy_revision: int | None,
        requested_by: str,
        blockers: tuple[str, ...],
        summary: Mapping[str, object],
    ) -> dict[str, Any]:
        timestamp = utc_timestamp()
        operation_id = str(uuid4())
        state = "blocked" if blockers else "ready"
        self.connection.execute(
            "INSERT INTO certificate_operations("
            "id,cluster_id,operation_type,state,trust_domain_ids_json,request_hash,policy_revision,phase,blockers_json,"
            "summary_json,requested_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                operation_id,
                cluster_id,
                operation_type,
                state,
                _json(list(trust_domain_ids)),
                request_hash,
                policy_revision,
                "preview",
                _json(list(blockers)),
                _json(dict(summary)),
                requested_by,
                timestamp,
                timestamp,
            ),
        )
        row = self.connection.execute("SELECT * FROM certificate_operations WHERE id=?", (operation_id,)).fetchone()
        return self._operation(row)

    def list_operations(self, cluster_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM certificate_operations WHERE cluster_id=? ORDER BY created_at DESC,id DESC", (cluster_id,)
        ).fetchall()
        return [self._operation(row) for row in rows]

    def get_operation(self, operation_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM certificate_operations WHERE id=?", (operation_id,)).fetchone()
        if row is None:
            raise CertificateNotFound("Certificate operation not found")
        result = self._operation(row)
        steps = self.connection.execute(
            "SELECT * FROM certificate_operation_steps WHERE operation_id=? ORDER BY sequence", (operation_id,)
        ).fetchall()
        result["steps"] = [
            {
                "key": item["step_key"],
                "sequence": item["sequence"],
                "phase": item["phase"],
                "target_id": item["target_id"],
                "command_type": item["command_type"],
                "state": item["state"],
                "attempt_count": item["attempt_count"],
                "result": _load_json(item["result_json"], {}),
            }
            for item in steps
        ]
        return result

    @staticmethod
    def _domain(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "cluster_id": row["cluster_id"],
            "kind": row["kind"],
            "state": row["state"],
            "legacy_shared": bool(row["legacy_shared"]),
            "split_migration_state": row["split_migration_state"],
            "compatibility_profile": row["compatibility_profile"],
            "verification_mode": row["verification_mode"],
            "revision": row["revision"],
        }

    @staticmethod
    def _policy(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "cluster_id": row["cluster_id"],
            "trust_domain_id": row["trust_domain_id"],
            "revision": row["revision"],
            **_load_json(row["policy_json"], dict(DEFAULT_CERTIFICATE_POLICY)),
        }

    @staticmethod
    def _asset(row: sqlite3.Row, *, include_domain: bool = False) -> dict[str, Any]:
        result = {
            "id": row["id"],
            "cluster_id": row["cluster_id"],
            "trust_domain_id": row["trust_domain_id"],
            "owner_type": row["owner_type"],
            "owner_id": row["owner_id"],
            "purpose": row["purpose"],
            "provider_type": row["provider_type"],
            "management_state": row["management_state"],
            "storage_locator": _load_json(row["storage_locator_json"], {}),
            "desired_identity": _load_json(row["desired_identity_json"], {}),
            "active_generation_id": row["active_generation_id"],
            "health": row["health_state"],
            "last_observed_at": row["last_observed_at"],
        }
        if include_domain:
            result.update(
                {
                    "trust_domain": row["trust_domain_kind"],
                    "legacy_shared": bool(row["legacy_shared"]),
                    "split_migration_state": row["split_migration_state"],
                }
            )
        return result

    @staticmethod
    def _consumer(row: sqlite3.Row, *, include_domain: bool = False) -> dict[str, Any]:
        result = {
            "id": row["id"],
            "cluster_id": row["cluster_id"],
            "trust_domain_id": row["trust_domain_id"],
            "consumer_type": row["consumer_type"],
            "consumer_kind": row["consumer_kind"],
            "owner_id": row["owner_id"],
            "description": row["description"],
            "verification_method": row["verification_method"],
            "trust_state": row["trust_state"],
            "candidate_trust_state": row["candidate_trust_state"],
            "last_verified_at": row["last_verified_at"],
            "attestation_expires_at": row["attestation_expires_at"],
            "revision": row["revision"],
            "blocking_reason": row["blocking_reason"],
        }
        if include_domain:
            result["trust_domain"] = row["trust_domain_kind"]
        return result

    @staticmethod
    def _generation(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "asset_id": row["asset_id"],
            "fingerprint": row["certificate_fingerprint"],
            "metadata": _load_json(row["public_metadata_json"], {}),
            "chain_fingerprints": _load_json(row["chain_fingerprints_json"], []),
            "source": row["source"],
            "state": row["state"],
            "format": row["certificate_format"],
            "created_at": row["created_at"],
            "verified_at": row["verified_at"],
        }

    @staticmethod
    def _observation(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "asset_id": row["asset_id"],
            "generation_fingerprint": row["generation_fingerprint"],
            "metadata": _load_json(row["metadata_json"], {}),
            "validation": _load_json(row["validation_json"], {}),
            "endpoint": _load_json(row["endpoint_json"], {}),
            "source": row["source"],
            "error_code": row["error_code"],
            "error_message": row["error_message"],
            "observed_at": row["observed_at"],
        }

    @staticmethod
    def _operation(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "cluster_id": row["cluster_id"],
            "operation_type": row["operation_type"],
            "state": row["state"],
            "revision": row["revision"],
            "trust_domain_ids": _load_json(row["trust_domain_ids_json"], []),
            "request_hash": row["request_hash"],
            "policy_revision": row["policy_revision"],
            "run_id": row["run_id"],
            "maintenance_plan_id": row["maintenance_plan_id"],
            "phase": row["phase"],
            "blockers": _load_json(row["blockers_json"], []),
            "summary": _load_json(row["summary_json"], {}),
            "requested_by": row["requested_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "completed_at": row["completed_at"],
        }


__all__ = [
    "CertificateRepository",
    "SCHEMA_TABLES",
    "install_certificate_schema",
    "utc_timestamp",
]
