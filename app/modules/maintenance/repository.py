"""Read-only persistence boundary for maintenance planning.

Maintenance planning often needs a small view of records owned by hosts,
clusters, workloads, and platform runs.  This repository keeps those reads in
one public contract while leaving writes and legacy maintenance storage in
place for the incremental refactor.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import sqlite3
from typing import Any, Callable, Iterator, Mapping

from app.modules.platform import open_config, update_run_status_in_connection

from .lifecycle import LockScope


def _memory_bytes(value: object) -> int | None:
    """Parse the persisted workload memory form without exposing its config."""

    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if len(text) < 2 or text[-1] not in {"k", "m", "g", "t"}:
        return None
    try:
        amount = float(text[:-1])
    except ValueError:
        return None
    if amount <= 0:
        return None
    units = {"k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}
    return int(amount * units[text[-1]])


def _managed_storage_path(value: object) -> bool:
    """Recognize only the explicit non-system paths accepted by workloads."""

    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or value == "/"
        or ":" in value
        or any(character.isspace() for character in value)
    ):
        return False
    blocked = ("/etc", "/usr", "/bin", "/sbin", "/lib", "/lib64", "/boot", "/proc", "/sys", "/dev", "/run", "/tmp")
    return not any(value == path or value.startswith(path + "/") for path in blocked)


@dataclass(frozen=True)
class HostLookup:
    id: int
    name: str
    enabled: bool
    record: Mapping[str, Any]


@dataclass(frozen=True)
class HostRuntimeLookup:
    """Read-only host health projection owned by observability."""

    node_id: int
    record: Mapping[str, Any]


@dataclass(frozen=True)
class WorkloadLookup:
    id: int
    cluster_id: int
    node_id: int
    role: str
    revision: int
    record: Mapping[str, Any]


@dataclass(frozen=True)
class ClusterLookup:
    id: int
    name: str
    record: Mapping[str, Any]


@dataclass(frozen=True)
class RunLookup:
    id: int
    status: str
    target: str
    record: Mapping[str, Any]


@dataclass(frozen=True)
class ConflictObservation:
    """Read-only conflict scope assembled from owned public projections."""

    node_id: int
    cluster_ids: tuple[int, ...]
    host_assignment_ids: tuple[int, ...]
    conflict_identifiers: tuple[str, ...]

    @property
    def has_conflicts(self) -> bool:
        return bool(self.conflict_identifiers)


class MaintenanceRepository:
    """Typed read contract usable with a DB factory or active connection."""

    def __init__(self, db_factory: Callable[[], Any] | None = None, *, connection: Any | None = None):
        if db_factory is None and connection is None:
            raise ValueError("MaintenanceRepository requires a database factory or connection")
        if db_factory is not None and connection is not None:
            raise ValueError("Provide either a database factory or connection, not both")
        self._db_factory = db_factory
        self._connection = connection

    @classmethod
    def from_connection(cls, connection: Any) -> "MaintenanceRepository":
        return cls(connection=connection)

    @contextmanager
    def _db(self) -> Iterator[Any]:
        if self._connection is not None:
            yield self._connection
            return
        assert self._db_factory is not None
        with self._db_factory() as connection:
            yield connection

    def host(self, node_id: int) -> HostLookup | None:
        with self._db() as connection:
            row = connection.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
        if not row:
            return None
        record = dict(row)
        return HostLookup(int(record["id"]), str(record["name"]), bool(record["enabled"]), record)

    def host_runtime(self, node_id: int) -> HostRuntimeLookup | None:
        """Return the latest durable host observation without exposing SQL."""

        with self._db() as connection:
            row = connection.execute(
                "SELECT * FROM host_runtime_observations WHERE node_id=?",
                (node_id,),
            ).fetchone()
        return HostRuntimeLookup(node_id, dict(row)) if row else None

    def cluster_exists(self, cluster_id: int) -> bool:
        """Check cluster existence through the maintenance read contract."""

        with self._db() as connection:
            return connection.execute("SELECT 1 FROM clusters WHERE id=?", (cluster_id,)).fetchone() is not None

    def active_workloads_for_node(self, node_id: int) -> tuple[WorkloadLookup, ...]:
        with self._db() as connection:
            rows = connection.execute(
                "SELECT * FROM cluster_assignments WHERE node_id=? AND state='active' ORDER BY id",
                (node_id,),
            ).fetchall()
        return tuple(
            WorkloadLookup(
                id=int(row["id"]),
                cluster_id=int(row["cluster_id"]),
                node_id=int(row["node_id"]),
                role=str(row["role"]),
                revision=int(row["revision"]),
                record=dict(row),
            )
            for row in rows
        )

    def clusters(self, cluster_ids: tuple[int, ...]) -> tuple[ClusterLookup, ...]:
        if not cluster_ids:
            return ()
        placeholders = ",".join("?" for _ in cluster_ids)
        with self._db() as connection:
            rows = connection.execute(
                "SELECT * FROM clusters WHERE id IN (" + placeholders + ") ORDER BY id",
                cluster_ids,
            ).fetchall()
        return tuple(ClusterLookup(int(row["id"]), str(row["name"]), dict(row)) for row in rows)

    def evacuation_inventory(
        self,
        *,
        cluster_id: int,
        source_node_id: int,
        replacement_node_id: int | None,
        max_surge: int,
    ) -> dict[str, Any]:
        """Assemble the narrow, read-only inventory used by evacuation preview.

        This is an approved maintenance read projection.  It intentionally
        exposes summarized workload resources only, never encrypted configs or
        controller credentials, and performs no plan, run, lock, or remote I/O.
        """

        with self._db() as connection:
            cluster_row = connection.execute("SELECT * FROM clusters WHERE id=?", (cluster_id,)).fetchone()
            source_row = connection.execute("SELECT * FROM nodes WHERE id=?", (source_node_id,)).fetchone()
            replacement_row = (
                connection.execute("SELECT * FROM nodes WHERE id=?", (replacement_node_id,)).fetchone()
                if replacement_node_id is not None
                else None
            )
            source_membership = connection.execute(
                "SELECT * FROM memberships WHERE cluster_id=? AND node_id=?",
                (cluster_id, source_node_id),
            ).fetchone()
            replacement_membership = (
                connection.execute(
                    "SELECT * FROM memberships WHERE cluster_id=? AND node_id=?",
                    (cluster_id, replacement_node_id),
                ).fetchone()
                if replacement_node_id is not None
                else None
            )
            source_assignments = connection.execute(
                "SELECT * FROM cluster_assignments WHERE cluster_id=? AND node_id=? AND state='active' ORDER BY id",
                (cluster_id, source_node_id),
            ).fetchall()
            replacement_assignments = (
                connection.execute(
                    "SELECT * FROM cluster_assignments WHERE node_id=? AND state='active' ORDER BY id",
                    (replacement_node_id,),
                ).fetchall()
                if replacement_node_id is not None
                else []
            )
            related_cluster_ids = sorted({
                cluster_id,
                *(int(row["cluster_id"]) for row in replacement_assignments),
            })
            placeholders = ",".join("?" for _ in related_cluster_ids)
            related_clusters = connection.execute(
                "SELECT * FROM clusters WHERE id IN (" + placeholders + ") ORDER BY id",
                related_cluster_ids,
            ).fetchall()
            source_runtime = connection.execute(
                "SELECT * FROM host_runtime_observations WHERE node_id=?", (source_node_id,)
            ).fetchone()
            replacement_runtime = (
                connection.execute(
                    "SELECT * FROM host_runtime_observations WHERE node_id=?", (replacement_node_id,)
                ).fetchone()
                if replacement_node_id is not None
                else None
            )
            source_observations = {}
            if source_assignments:
                assignment_ids = [int(row["id"]) for row in source_assignments]
                observation_rows = connection.execute(
                    "SELECT * FROM workload_observations WHERE assignment_id IN ("
                    + ",".join("?" for _ in assignment_ids) + ")",
                    assignment_ids,
                ).fetchall()
                source_observations = {int(row["assignment_id"]): dict(row) for row in observation_rows}

        return {
            "cluster": self._public_cluster_projection(cluster_row),
            "clusters": [self._public_cluster_projection(row) for row in related_clusters],
            "source": dict(source_row) if source_row else {},
            "replacement": dict(replacement_row) if replacement_row else {},
            "source_node_id": source_node_id,
            "replacement_node_id": replacement_node_id,
            "source_membership": dict(source_membership) if source_membership else {},
            "replacement_membership": dict(replacement_membership) if replacement_membership else {},
            "source_runtime": self._public_runtime_projection(source_runtime),
            "replacement_runtime": self._public_runtime_projection(replacement_runtime),
            "source_assignments": [
                self._public_evacuation_assignment(row, source_observations.get(int(row["id"])))
                for row in source_assignments
            ],
            "replacement_assignments": [
                {"cluster_id": int(row["cluster_id"]), "role": str(row["role"])}
                for row in replacement_assignments
            ],
            "max_surge": max_surge,
        }

    @staticmethod
    def _public_cluster_projection(row: Any | None) -> dict[str, Any]:
        if not row:
            return {}
        record = dict(row)
        try:
            role_ports = json.loads(record.get("role_ports_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            role_ports = {}
        try:
            zoning = json.loads(record.get("zoning_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            zoning = {}
        return {
            "id": int(record["id"]),
            "provider_type": record.get("provider_type", "native_podman"),
            "role_ports": role_ports,
            "zoning": zoning,
        }

    @staticmethod
    def _public_runtime_projection(row: Any | None) -> dict[str, Any] | None:
        if not row:
            return None
        record = dict(row)
        try:
            interfaces = json.loads(record.get("network_interfaces_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            interfaces = {}
        return {
            "initialized": bool(record.get("initialized")),
            "reachable": bool(record.get("reachable")),
            "network_interfaces": interfaces,
        }

    @staticmethod
    def _public_evacuation_assignment(row: Any, observation: Mapping[str, Any] | None) -> dict[str, Any]:
        resource = {"storage_managed": False}
        try:
            config = json.loads(open_config(str(row["config_json"])))
            if isinstance(config, dict):
                cpu = config.get("cpu")
                memory = _memory_bytes(config.get("memory"))
                path = config.get("storage_path")
                resource = {
                    "cpu": cpu,
                    "memory_bytes": memory,
                    "storage_managed": _managed_storage_path(path),
                }
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
        return {
            "id": int(row["id"]),
            "cluster_id": int(row["cluster_id"]),
            "role": str(row["role"]),
            "resource": resource,
            "observation": dict(observation or {}),
        }

    def run(self, run_id: int) -> RunLookup | None:
        with self._db() as connection:
            row = connection.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            return None
        record = dict(row)
        return RunLookup(int(record["id"]), str(record["status"]), str(record["target"]), record)

    def mark_run_running(self, run_id: int) -> None:
        """Re-open an attached run without changing its identity or log."""

        self.mark_run_status(run_id, "running")

    def mark_run_status(
        self,
        run_id: int,
        status: str,
        *,
        finished_at: str | None = None,
        log_suffix: str = "",
    ) -> None:
        """Persist a maintenance run state through the owned write boundary."""

        allowed = {"queued", "running", "succeeded", "failed", "cancelled", "recovery_required"}
        if status not in allowed:
            raise ValueError(f"Unsupported run status: {status}")
        with self._db() as connection:
            update_run_status_in_connection(
                connection,
                run_id,
                status,
                finished_at=finished_at,
                log_suffix=log_suffix,
            )

    def has_scope_conflict_in_connection(
        self,
        connection,
        *,
        cluster_id: int | None = None,
        node_id: int | None = None,
        assignment_id: int | None = None,
    ) -> bool:
        """Check maintenance plans/locks over the expanded host-cluster scope.

        This is an explicitly declared maintenance read projection. It never
        mutates host, cluster, or workload records.
        """

        cluster_ids = {int(cluster_id)} if cluster_id is not None else set()
        node_ids = {int(node_id)} if node_id is not None else set()
        assignment_ids = {int(assignment_id)} if assignment_id is not None else set()
        if assignment_ids:
            placeholders = ",".join("?" for _ in assignment_ids)
            rows = connection.execute(
                "SELECT id,cluster_id,node_id FROM cluster_assignments WHERE id IN (" + placeholders + ")",
                tuple(sorted(assignment_ids)),
            ).fetchall()
            cluster_ids.update(int(row["cluster_id"]) for row in rows)
            node_ids.update(int(row["node_id"]) for row in rows)
        if node_ids:
            placeholders = ",".join("?" for _ in node_ids)
            parameters = tuple(sorted(node_ids))
            cluster_ids.update(
                int(row["cluster_id"])
                for row in connection.execute(
                    "SELECT cluster_id FROM memberships WHERE node_id IN (" + placeholders + ")",
                    parameters,
                ).fetchall()
            )
            cluster_ids.update(
                int(row["cluster_id"])
                for row in connection.execute(
                    "SELECT cluster_id FROM cluster_assignments WHERE node_id IN (" + placeholders + ")",
                    parameters,
                ).fetchall()
            )
        if cluster_ids:
            placeholders = ",".join("?" for _ in cluster_ids)
            parameters = tuple(sorted(cluster_ids))
            rows = connection.execute(
                "SELECT id,node_id FROM cluster_assignments WHERE cluster_id IN (" + placeholders + ")",
                parameters,
            ).fetchall()
            assignment_ids.update(int(row["id"]) for row in rows)
            node_ids.update(int(row["node_id"]) for row in rows)
            node_ids.update(
                int(row["node_id"])
                for row in connection.execute(
                    "SELECT node_id FROM memberships WHERE cluster_id IN (" + placeholders + ")",
                    parameters,
                ).fetchall()
            )
        clauses: list[str] = []
        parameters: list[int] = []
        for column, values in (
            ("target_node_id", node_ids),
            ("target_cluster_id", cluster_ids),
            ("target_assignment_id", assignment_ids),
        ):
            if values:
                clauses.append(column + " IN (" + ",".join("?" for _ in values) + ")")
                parameters.extend(sorted(values))
        if clauses and connection.execute(
            "SELECT 1 FROM maintenance_plans WHERE lifecycle_state IN "
            "('ready','executing','paused','recovery_required') AND (" + " OR ".join(clauses) + ") LIMIT 1",
            parameters,
        ).fetchone():
            return True
        lock_scopes: list[str] = []
        lock_parameters: list[str] = []
        for scope, values in (
            ("host", node_ids),
            ("cluster", cluster_ids),
            ("assignment", assignment_ids),
        ):
            if values:
                lock_scopes.append("(scope_kind=? AND scope_id IN (" + ",".join("?" for _ in values) + "))")
                lock_parameters.extend((scope, *(str(value) for value in sorted(values))))
        return bool(lock_scopes and connection.execute(
            "SELECT 1 FROM maintenance_locks WHERE released_at IS NULL AND ("
            + " OR ".join(lock_scopes) + ") LIMIT 1",
            lock_parameters,
        ).fetchone())

    def observe_conflicts_in_connection(
        self,
        connection,
        node_id: int,
        *,
        exclude_plan_id: str | None = None,
        exclude_run_id: int | None = None,
    ) -> ConflictObservation:
        """Return a maintenance-only conflict projection without remote mutation."""

        node = connection.execute("SELECT id,name FROM nodes WHERE id=?", (node_id,)).fetchone()
        if not node:
            raise KeyError(node_id)
        membership_clusters = {
            row["cluster_id"]
            for row in connection.execute("SELECT cluster_id FROM memberships WHERE node_id=?", (node_id,))
        }
        host_assignments = connection.execute(
            "SELECT id,cluster_id FROM cluster_assignments WHERE node_id=?", (node_id,),
        ).fetchall()
        cluster_ids = membership_clusters | {row["cluster_id"] for row in host_assignments}
        host_assignment_ids = {row["id"] for row in host_assignments}
        cluster_assignments = []
        if cluster_ids:
            placeholders = ",".join("?" for _ in cluster_ids)
            cluster_assignments = connection.execute(
                "SELECT id,cluster_id,operation_run_id FROM cluster_assignments WHERE cluster_id IN ("
                + placeholders + ")",
                tuple(sorted(cluster_ids)),
            ).fetchall()
        cluster_assignment_ids = {row["id"] for row in cluster_assignments}
        cluster_scope_ids = {str(value) for value in cluster_ids}
        assignment_scope_ids = {str(value) for value in cluster_assignment_ids}
        cluster_names = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM clusters WHERE id IN (" + ",".join("?" for _ in cluster_ids) + ")",
                tuple(sorted(cluster_ids)),
            ).fetchall()
        } if cluster_ids else set()

        active_plan_rows = connection.execute("""
            SELECT id,run_id,target_node_id,target_cluster_id,target_assignment_id
            FROM maintenance_plans
            WHERE lifecycle_state IN ('ready','executing','paused','recovery_required')
        """).fetchall()
        plan_rows = {row["id"]: row for row in active_plan_rows}
        relevant_plan_ids = {
            row["id"] for row in active_plan_rows
            if row["id"] != exclude_plan_id and (
                row["target_node_id"] == node_id
                or row["target_cluster_id"] in cluster_ids
                or row["target_assignment_id"] in cluster_assignment_ids
            )
        }
        identifiers = {f"maintenance-plan:{plan_id}" for plan_id in relevant_plan_ids}

        lock_rows = connection.execute("""
            SELECT id,scope_kind,scope_id,owner_plan_id
            FROM maintenance_locks
            WHERE released_at IS NULL
        """).fetchall()
        for row in lock_rows:
            if row["owner_plan_id"] == exclude_plan_id:
                continue
            scope = row["scope_kind"]
            scope_id = row["scope_id"]
            if not (
                (scope == LockScope.HOST.value and scope_id == str(node_id))
                or (scope == LockScope.CLUSTER.value and scope_id in cluster_scope_ids)
                or (scope == LockScope.ASSIGNMENT.value and scope_id in assignment_scope_ids)
                or row["owner_plan_id"] in relevant_plan_ids
            ):
                continue
            relevant_plan_ids.add(row["owner_plan_id"])
            identifiers.add(f"maintenance-plan:{row['owner_plan_id']}")
            identifiers.add(f"maintenance-lock:{scope}:{scope_id}:{row['id']}")

        excluded_run_ids = {exclude_run_id} if exclude_run_id is not None else set()
        if exclude_plan_id and exclude_plan_id in plan_rows and plan_rows[exclude_plan_id]["run_id"] is not None:
            excluded_run_ids.add(plan_rows[exclude_plan_id]["run_id"])
        linked_run_ids = {
            plan_rows[plan_id]["run_id"]
            for plan_id in relevant_plan_ids
            if plan_id in plan_rows and plan_rows[plan_id]["run_id"] is not None
        }
        active_run_rows = connection.execute("""
            SELECT id,target,context_json
            FROM runs
            WHERE status IN ('queued','running','recovery_required')
        """).fetchall()
        active_run_ids = {row["id"] for row in active_run_rows}

        for row in cluster_assignments:
            run_id = row["operation_run_id"]
            if run_id is None or run_id not in active_run_ids or run_id in excluded_run_ids:
                continue
            linked_run_ids.add(run_id)
            identifiers.add(f"assignment-operation:{row['id']}:{run_id}")
        if cluster_ids:
            placeholders = ",".join("?" for _ in cluster_ids)
            batch_rows = connection.execute(
                "SELECT run_id,cluster_id FROM workload_change_batches WHERE cluster_id IN (" + placeholders + ")",
                tuple(sorted(cluster_ids)),
            ).fetchall()
            for row in batch_rows:
                if row["run_id"] in excluded_run_ids:
                    continue
                linked_run_ids.add(row["run_id"])
                identifiers.add(f"workload-batch:{row['run_id']}")

        scoped_names = {node["name"], *cluster_names}
        for row in active_run_rows:
            run_id = row["id"]
            if run_id in excluded_run_ids:
                continue
            try:
                context = json.loads(row["context_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                context = {}
            if not isinstance(context, dict):
                context = {}

            def context_ids(singular: str, plural: str) -> set[int]:
                values = [context.get(singular)]
                multiple = context.get(plural)
                if isinstance(multiple, (list, tuple)):
                    values.extend(multiple)
                return {
                    int(value) for value in values
                    if not isinstance(value, bool)
                    and (isinstance(value, int) or (isinstance(value, str) and value.isdigit()))
                }

            target = row["target"] or ""
            target_matches = any(target == name or target.startswith(name + ":") for name in scoped_names)
            context_matches = (
                node_id in context_ids("node_id", "node_ids")
                or bool(cluster_ids.intersection(context_ids("cluster_id", "cluster_ids")))
                or bool(cluster_assignment_ids.intersection(context_ids("assignment_id", "assignment_ids")))
            )
            if run_id in linked_run_ids or target_matches or context_matches:
                identifiers.add(f"run:{run_id}")

        return ConflictObservation(
            node_id=node_id,
            cluster_ids=tuple(sorted(cluster_ids)),
            host_assignment_ids=tuple(sorted(host_assignment_ids)),
            conflict_identifiers=tuple(sorted(identifiers)),
        )
