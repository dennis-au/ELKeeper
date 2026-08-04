from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from .models import (
    ClusterObservation,
    HostObservation,
    MaintenanceBackend,
    ProviderType,
    RevisionObservation,
    SourceObservation,
    SourceStatus,
    WorkloadObservation,
)
from .service import HostRebootPlanningData
from .provider import provider_profile_from_record
from .repository import MaintenanceRepository


ELASTICSEARCH_ROLES = frozenset(("master", "hot", "warm", "ml", "ingest", "coordinating"))
ENDPOINT_ROLES = frozenset(("master", "coordinating", "kibana", "fleet-server", "logstash"))
ROLE_DATA_TIERS = {
    "hot": ("content", "hot"),
    "warm": ("content", "warm"),
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_observed_at(value: str | datetime | None, fallback: datetime) -> tuple[datetime, bool]:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return fallback, False
    else:
        return fallback, False
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc), True


def _source(
    name: str,
    status: SourceStatus,
    observed_at: datetime,
    *,
    required: bool = True,
    error_category: str | None = None,
) -> SourceObservation:
    return SourceObservation(
        source=name,
        status=status,
        observed_at=observed_at,
        required=required,
        error_category=error_category,
    )


def _host_runtime(connection: sqlite3.Connection, telemetry: Any, node_id: int) -> dict:
    state = getattr(telemetry, "host_states", {}).get(node_id)
    if state:
        return dict(state)
    row = connection.execute(
        "SELECT * FROM host_runtime_observations WHERE node_id=?", (node_id,),
    ).fetchone()
    if not row:
        return {}
    result = dict(row)
    if "network_interfaces" not in result:
        try:
            result["network_interfaces"] = json.loads(result.get("network_interfaces_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            result["network_interfaces"] = {}
    return result


def _cluster_runtime(telemetry: Any, cluster_id: int) -> dict:
    state = getattr(telemetry, "cluster_states", {}).get(cluster_id)
    return dict(state) if state else {}


def _observed_network_interfaces(state: dict) -> dict[str, set[str]]:
    raw = state.get("network_interfaces")
    if not isinstance(raw, Mapping):
        return {}
    observed = {}
    for interface, values in raw.items():
        if not isinstance(interface, str) or not interface:
            continue
        if isinstance(values, Mapping):
            values = values.get("addresses", ())
        if isinstance(values, str):
            values = (values,)
        if not isinstance(values, (list, tuple, set, frozenset)):
            continue
        addresses = {
            value.split("/", 1)[0].strip()
            for value in values
            if isinstance(value, str) and value.split("/", 1)[0].strip()
        }
        if addresses:
            observed[interface] = addresses
    return observed


def _membership_ready(
    connection: sqlite3.Connection,
    node_id: int,
    cluster_ids: tuple[int, ...],
    state: dict,
) -> bool:
    if not cluster_ids:
        return True
    observed_interfaces = _observed_network_interfaces(state)
    if not observed_interfaces:
        return False
    placeholders = ",".join("?" for _ in cluster_ids)
    rows = connection.execute(
        "SELECT * FROM memberships WHERE node_id=? AND cluster_id IN (" + placeholders + ")",
        (node_id, *cluster_ids),
    ).fetchall()
    if len(rows) != len(cluster_ids):
        return False
    for row in rows:
        mode = row["network_mode"]
        data_interface = row["data_interface"]
        data_address = row["data_address"]
        user_interface = row["user_interface"]
        user_address = row["user_address"]
        if not all((data_interface, data_address, user_interface, user_address)):
            return False
        if mode == "shared":
            if data_interface != user_interface or data_address != user_address:
                return False
        elif mode == "dedicated":
            if data_interface == user_interface or data_address == user_address:
                return False
        else:
            return False
        if data_address not in observed_interfaces.get(data_interface, set()):
            return False
        if user_address not in observed_interfaces.get(user_interface, set()):
            return False
    return True


def _configured_cluster_uuid(row: sqlite3.Row) -> str | None:
    keys = set(row.keys())
    for column in ("configured_uuid", "expected_cluster_uuid"):
        if column in keys and row[column]:
            return str(row[column])
    return None


def _disk_watermarks_safe(state: dict, data_affected: bool) -> bool:
    if not data_affected:
        return True
    try:
        total = int(state.get("disk_total_bytes") or 0)
        available = int(state.get("disk_available_bytes") or 0)
    except (TypeError, ValueError):
        return False
    if total <= 0 or available < 0 or available > total:
        return False
    return ((total - available) / total) < 0.9


def collect_host_reboot_planning_data(
    connection: sqlite3.Connection,
    telemetry: Any,
    *,
    node_id: int,
    capability_revision: str,
    conflicting_operations: tuple[str, ...] = (),
    node_shutdown_backend_enabled: bool = False,
    clock=utc_now,
) -> HostRebootPlanningData:
    captured_at = clock().astimezone(timezone.utc)
    repository = MaintenanceRepository.from_connection(connection)
    target = repository.host(node_id)
    if not target:
        raise KeyError(node_id)

    target_assignments = repository.active_workloads_for_node(node_id)
    affected_cluster_ids = tuple(sorted({row.cluster_id for row in target_assignments}))
    if affected_cluster_ids:
        placeholders = ",".join("?" for _ in affected_cluster_ids)
        assignment_rows = connection.execute(
            "SELECT cluster_assignments.*,nodes.name AS node_name,"
            "workload_observations.running AS observed_running,workload_observations.observed_at,"
            "workload_observations.error AS observation_error "
            "FROM cluster_assignments JOIN nodes ON nodes.id=cluster_assignments.node_id "
            "LEFT JOIN workload_observations ON workload_observations.assignment_id=cluster_assignments.id "
            "WHERE cluster_assignments.cluster_id IN (" + placeholders + ") "
            "AND cluster_assignments.state='active' ORDER BY cluster_assignments.id",
            affected_cluster_ids,
        ).fetchall()
        cluster_rows = connection.execute(
            "SELECT * FROM clusters WHERE id IN (" + placeholders + ") ORDER BY id",
            affected_cluster_ids,
        ).fetchall()
    else:
        assignment_rows = []
        cluster_rows = []

    host_ids = tuple(sorted({node_id, *(row["node_id"] for row in assignment_rows)}))
    placeholders = ",".join("?" for _ in host_ids)
    node_rows = connection.execute(
        "SELECT * FROM nodes WHERE id IN (" + placeholders + ") ORDER BY id", host_ids,
    ).fetchall()
    memberships_by_node = {
        current_node_id: tuple(sorted({
            row["cluster_id"] for row in assignment_rows if row["node_id"] == current_node_id
        }))
        for current_node_id in host_ids
    }

    host_observations = []
    runtime_missing = False
    runtime_error = False
    runtime_observation_invalid = False
    for row in node_rows:
        state = _host_runtime(connection, telemetry, row["id"])
        observed_value = state.get("observed_at")
        observed_at, observed_at_valid = parse_observed_at(observed_value, captured_at)
        if observed_value is None or observed_value == "":
            runtime_missing = True
        elif not observed_at_valid:
            runtime_observation_invalid = True
        if state.get("last_error"):
            runtime_error = True
        host_observations.append(HostObservation(
            node_id=row["id"],
            name=row["name"],
            enabled=bool(row["enabled"]),
            initialized=bool(state.get("initialized", False)),
            reachable=bool(state.get("reachable", False)),
            membership_ready=_membership_ready(
                connection, row["id"], memberships_by_node[row["id"]], state,
            ),
            observed_at=observed_at,
        ))

    workload_observations = []
    revisions = []
    workload_missing = False
    workload_observation_invalid = False
    for row in assignment_rows:
        observed_value = row["observed_at"]
        observed_at, observed_at_valid = parse_observed_at(observed_value, captured_at)
        if observed_value is None or observed_value == "":
            workload_missing = True
        elif not observed_at_valid:
            workload_observation_invalid = True
        running = bool(row["observed_running"])
        error = str(row["observation_error"] or "")
        role = row["role"]
        workload_observations.append(WorkloadObservation(
            assignment_id=row["id"],
            cluster_id=row["cluster_id"],
            node_id=row["node_id"],
            name=f"{row['node_name']} {role}",
            role=role,
            expected_running=True,
            running=running,
            ready=running and not error,
            master_eligible=role == "master",
            data_tiers=ROLE_DATA_TIERS.get(role, ()),
            endpoint_required=role in ENDPOINT_ROLES,
            observed_at=observed_at,
        ))
        revisions.append(RevisionObservation(assignment_id=row["id"], revision=row["revision"]))

    selected_data_clusters = {
        row.cluster_id for row in target_assignments if row.role in ROLE_DATA_TIERS
    }
    cluster_observations = []
    elasticsearch_missing = False
    elasticsearch_error = False
    elasticsearch_observation_invalid = False
    shard_safety_missing = False
    identity_missing = False
    identity_mismatch = False
    for row in cluster_rows:
        provider = provider_profile_from_record(row)
        state = _cluster_runtime(telemetry, row["id"])
        observed_value = state.get("observed_at")
        observed_at, observed_at_valid = parse_observed_at(observed_value, captured_at)
        if observed_value is None or observed_value == "":
            elasticsearch_missing = True
        elif not observed_at_valid:
            elasticsearch_observation_invalid = True
        if state.get("last_error"):
            elasticsearch_error = True
        cluster_workloads = [item for item in workload_observations if item.cluster_id == row["id"]]
        data_affected = row["id"] in selected_data_clusters
        shard_safety_observed = bool(state.get("shard_safety_observed", False))
        if data_affected and not shard_safety_observed:
            shard_safety_missing = True
        health = state.get("status", "unknown")
        if health not in {"green", "yellow", "red"}:
            health = "unknown"
        configured_uuid = _configured_cluster_uuid(row)
        observed_name = state.get("cluster_name") or None
        observed_uuid = state.get("cluster_uuid") or None
        if not configured_uuid or not observed_name or not observed_uuid:
            identity_missing = True
        elif observed_name != row["name"] or observed_uuid != configured_uuid:
            identity_mismatch = True
        cluster_observations.append(ClusterObservation(
            cluster_id=row["id"],
            provider_type=provider.provider_type,
            backend=provider.maintenance_backend,
            lifecycle_supported=(
                provider.capabilities.lifecycle_api
                and provider.maintenance_backend is not MaintenanceBackend.NONE
                and (
                    provider.maintenance_backend is not MaintenanceBackend.NODE_SHUTDOWN_API
                    or node_shutdown_backend_enabled
                )
            ),
            configured_name=row["name"],
            configured_uuid=configured_uuid,
            observed_name=observed_name if configured_uuid else None,
            observed_uuid=observed_uuid,
            health=health,
            master_eligible_total=sum(item.master_eligible for item in cluster_workloads),
            master_eligible_available=sum(item.master_eligible and item.ready for item in cluster_workloads),
            initializing_shards=max(int(state.get("initializing_shards") or 0), 0),
            relocating_shards=max(int(state.get("relocating_shards") or 0), 0),
            no_last_shard_copy=(bool(state.get("no_last_shard_copy")) if data_affected else True),
            primary_promotion_safe=(bool(state.get("primary_promotion_safe")) if data_affected else True),
            allocation_setting_captured=False,
            disk_watermarks_safe=_disk_watermarks_safe(state, data_affected),
            target_artifact_ready=True,
            version_transition_supported=True,
            snapshot_recovery_ready=True,
            stale_shutdown_record=bool(state.get("stale_shutdown_record", True)),
            observed_at=observed_at,
        ))

    runtime_invalid = runtime_observation_invalid or workload_observation_invalid
    runtime_status = (
        SourceStatus.ERROR if runtime_error or runtime_invalid
        else SourceStatus.MISSING if runtime_missing or workload_missing
        else SourceStatus.OK
    )
    runtime_error_category = (
        "runtime-observation-invalid" if runtime_invalid
        else "runtime-unavailable" if runtime_status != SourceStatus.OK
        else None
    )
    sources = [
        _source("inventory", SourceStatus.OK, captured_at),
        _source(
            "runtime",
            runtime_status,
            min((item.observed_at for item in host_observations), default=captured_at),
            error_category=runtime_error_category,
        ),
        _source(
            "membership",
            SourceStatus.OK if all(item.membership_ready for item in host_observations) else SourceStatus.ERROR,
            captured_at,
            error_category=None if all(item.membership_ready for item in host_observations) else "membership-not-ready",
        ),
    ]
    affected_es = any(row.role in ELASTICSEARCH_ROLES for row in target_assignments)
    if affected_es:
        es_status = (
            SourceStatus.ERROR if elasticsearch_error or elasticsearch_observation_invalid
            else SourceStatus.MISSING if elasticsearch_missing
            else SourceStatus.OK
        )
        es_error_category = (
            "elasticsearch-observation-invalid" if elasticsearch_observation_invalid
            else "elasticsearch-unavailable" if es_status != SourceStatus.OK
            else None
        )
        sources.append(_source(
            "elasticsearch",
            es_status,
            min((item.observed_at for item in cluster_observations), default=captured_at),
            error_category=es_error_category,
        ))
        identity_status = (
            SourceStatus.ERROR if identity_mismatch
            else SourceStatus.MISSING if identity_missing
            else SourceStatus.OK
        )
        sources.append(_source(
            "cluster-identity",
            identity_status,
            min((item.observed_at for item in cluster_observations), default=captured_at),
            error_category=(
                "cluster-identity-mismatch" if identity_mismatch
                else "configured-cluster-uuid-missing" if identity_missing
                else None
            ),
        ))
    if selected_data_clusters:
        sources.append(_source(
            "shard-safety",
            SourceStatus.MISSING if shard_safety_missing else SourceStatus.OK,
            min((item.observed_at for item in cluster_observations), default=captured_at),
            error_category="shard-safety-unobserved" if shard_safety_missing else None,
        ))

    return HostRebootPlanningData(
        target_node_id=node_id,
        captured_at=captured_at,
        capability_revision=capability_revision,
        sources=tuple(sources),
        hosts=tuple(host_observations),
        clusters=tuple(cluster_observations),
        workloads=tuple(workload_observations),
        assignment_revisions=tuple(revisions),
        conflicting_operations=conflicting_operations,
    )
