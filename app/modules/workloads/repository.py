"""Persistence boundary for cluster-qualified workload assignments."""

from __future__ import annotations

from contextlib import contextmanager
import json
from typing import Callable


class WorkloadRepository:
    def __init__(self, db_factory: Callable | None = None, *, connection=None):
        if db_factory is None and connection is None:
            raise ValueError("WorkloadRepository requires a database factory or connection")
        if db_factory is not None and connection is not None:
            raise ValueError("Provide either a database factory or connection, not both")
        self._db = db_factory
        self._connection = connection

    @classmethod
    def from_connection(cls, connection):
        return cls(connection=connection)

    @contextmanager
    def _connection_scope(self):
        if self._connection is not None:
            yield self._connection
            return
        assert self._db is not None
        with self._db() as connection:
            yield connection

    def active_count(self, cluster_id: int) -> int:
        with self._connection_scope() as connection:
            row = connection.execute(
                "SELECT count(*) AS count FROM cluster_assignments WHERE cluster_id=? AND state='active'",
                (cluster_id,),
            ).fetchone()
        return int(row["count"])

    def active_ids(self, cluster_id: int) -> list[int]:
        with self._connection_scope() as connection:
            rows = connection.execute(
                "SELECT id FROM cluster_assignments WHERE cluster_id=? AND state='active' ORDER BY id",
                (cluster_id,),
            ).fetchall()
        return [int(row["id"]) for row in rows]

    def upsert_assignment_in_connection(
        self,
        connection,
        *,
        cluster_id: int,
        node_id: int,
        role: str,
        config_json: str,
    ) -> int:
        """Create or reactivate one cluster-qualified assignment."""

        cursor = connection.execute(
            "INSERT INTO cluster_assignments(cluster_id,node_id,role,config_json) VALUES (?,?,?,?) "
            "ON CONFLICT(cluster_id,node_id,role) DO UPDATE SET config_json=excluded.config_json,state='active' RETURNING id",
            (cluster_id, node_id, role, config_json),
        )
        return int(cursor.fetchone()["id"])

    def update_assignment_config_in_connection(self, connection, assignment_id: int, config_json: str) -> None:
        """Persist an already-validated assignment configuration."""

        connection.execute(
            "UPDATE cluster_assignments SET config_json=? WHERE id=?",
            (config_json, assignment_id),
        )

    def initial_master_has_dependents_in_connection(
        self,
        connection,
        *,
        cluster_id: int,
        assignment_id: int,
    ) -> bool:
        """Protect the original master while other cluster workloads remain."""

        initial_master = connection.execute(
            "SELECT id FROM cluster_assignments WHERE cluster_id=? AND role='master' ORDER BY id LIMIT 1",
            (cluster_id,),
        ).fetchone()
        if not initial_master or int(initial_master["id"]) != assignment_id:
            return False
        return bool(
            connection.execute(
                "SELECT 1 FROM cluster_assignments WHERE cluster_id=? AND id<>?",
                (cluster_id, assignment_id),
            ).fetchone()
        )

    def delete_assignment_in_connection(self, connection, assignment_id: int) -> None:
        """Detach one assignment without touching its managed remote workload."""

        connection.execute("DELETE FROM cluster_assignments WHERE id=?", (assignment_id,))

    def active_for_node(self, node_id: int) -> list[dict]:
        """Return active assignment identities for a runtime host observation.

        The workload owner deliberately does not join cluster inventory here.
        Callers obtain cluster labels through the public cluster repository.
        """

        with self._connection_scope() as connection:
            rows = connection.execute(
                "SELECT id,cluster_id,node_id,role FROM cluster_assignments "
                "WHERE node_id=? AND state='active' ORDER BY id",
                (node_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def has_assignments_for_node(self, node_id: int) -> bool:
        """Report whether a host still owns any assignment, regardless of state."""

        with self._connection_scope() as connection:
            return connection.execute(
                "SELECT 1 FROM cluster_assignments WHERE node_id=? LIMIT 1",
                (node_id,),
            ).fetchone() is not None

    def active_for_cluster_in_connection(self, connection, cluster_id: int) -> list[dict]:
        """Return workload-owned active records without joining other domains."""

        rows = connection.execute(
            "SELECT * FROM cluster_assignments WHERE cluster_id=? AND state='active' ORDER BY id",
            (cluster_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def payload_assignments_in_connection(
        self,
        connection,
        cluster_id: int,
        *,
        included_ids: tuple[int, ...] = (),
    ) -> list[dict]:
        """Return active assignments plus explicitly included batch records.

        The projection is intentionally limited to the workload-owned table;
        callers resolve host and membership data through their public module
        repositories rather than joining foreign tables here.
        """

        ids = tuple(sorted({int(value) for value in included_ids}))
        clauses = ["cluster_id=?", "state='active'"]
        parameters: list[object] = [cluster_id]
        if ids:
            placeholders = ",".join("?" for _ in ids)
            clauses = ["cluster_id=?", f"(state='active' OR id IN ({placeholders}))"]
            parameters.extend(ids)
        rows = connection.execute(
            "SELECT * FROM cluster_assignments WHERE " + " AND ".join(clauses) + " ORDER BY id",
            parameters,
        ).fetchall()
        return [dict(row) for row in rows]

    def active_or_applying_for_node_outside_cluster_in_connection(
        self, connection, node_id: int, cluster_id: int
    ) -> list[dict]:
        rows = connection.execute(
            "SELECT * FROM cluster_assignments WHERE node_id=? AND cluster_id<>? "
            "AND state IN ('active','applying') ORDER BY id",
            (node_id, cluster_id),
        ).fetchall()
        return [dict(row) for row in rows]

    def active_or_applying_in_connection(self, connection) -> list[dict]:
        rows = connection.execute(
            "SELECT * FROM cluster_assignments WHERE state IN ('active','applying') ORDER BY id"
        ).fetchall()
        return [dict(row) for row in rows]

    def operation_run_ids_for_cluster_in_connection(self, connection, cluster_id: int) -> list[int]:
        rows = connection.execute(
            "SELECT operation_run_id FROM cluster_assignments WHERE cluster_id=? "
            "AND operation_run_id IS NOT NULL",
            (cluster_id,),
        ).fetchall()
        return [int(row["operation_run_id"]) for row in rows]

    def clear_operation_for_node_in_connection(self, connection, node_id: int, run_id: int) -> None:
        connection.execute(
            "UPDATE cluster_assignments SET operation_run_id=NULL WHERE node_id=? AND operation_run_id=?",
            (node_id, run_id),
        )

    def record_in_connection(self, connection, assignment_id: int) -> dict | None:
        row = connection.execute(
            "SELECT * FROM cluster_assignments WHERE id=?", (assignment_id,)
        ).fetchone()
        return dict(row) if row else None

    def revision_in_connection(self, connection, assignment_id: int) -> int | None:
        """Return the current revision used by maintenance plan validation."""

        row = connection.execute(
            "SELECT revision FROM cluster_assignments WHERE id=?", (assignment_id,)
        ).fetchone()
        return int(row["revision"]) if row else None

    def delete_assignment_in_connection(self, connection, assignment_id: int) -> None:
        connection.execute("DELETE FROM cluster_assignments WHERE id=?", (assignment_id,))

    def restore_config_in_connection(self, connection, assignment_id: int, config_encrypted: str) -> None:
        connection.execute(
            "UPDATE cluster_assignments SET config_json=? WHERE id=?",
            (config_encrypted, assignment_id),
        )

    def set_image_version_in_connection(self, connection, assignment_id: int, version: str) -> None:
        connection.execute(
            "UPDATE cluster_assignments SET image_version=? WHERE id=?",
            (version, assignment_id),
        )

    def finalize_batch_in_connection(self, connection, run_id: int, changes: list[dict], seal_config: Callable[[str], str]) -> None:
        """Commit a fully reconciled workload batch through the workload owner."""

        for item in changes:
            if item["kind"] == "create":
                connection.execute(
                    "UPDATE cluster_assignments SET state='active',operation_run_id=NULL "
                    "WHERE id=? AND operation_run_id=?",
                    (item["assignment_id"], run_id),
                )
            elif item["kind"] == "resources":
                connection.execute(
                    "UPDATE cluster_assignments SET config_json=?,revision=revision+1,operation_run_id=NULL "
                    "WHERE id=? AND operation_run_id=?",
                    (seal_config(json.dumps(item["config"])), item["assignment_id"], run_id),
                )
            else:
                connection.execute(
                    "DELETE FROM cluster_assignments WHERE id=? AND operation_run_id=?",
                    (item["assignment_id"], run_id),
                )

    def stage_batch_changes_in_connection(
        self,
        connection,
        cluster_id: int,
        run_id: int,
        changes: list[dict],
        seal_config: Callable[[str], str],
    ) -> None:
        """Claim existing work or create pending work for one guarded batch."""

        for item in changes:
            if item["kind"] == "create":
                cursor = connection.execute(
                    "INSERT INTO cluster_assignments(cluster_id,node_id,role,config_json,image_version,state,operation_run_id) "
                    "VALUES (?,?,?,?,?, 'applying',?)",
                    (
                        cluster_id,
                        item["node_id"],
                        item["role"],
                        seal_config(json.dumps(item["config"])),
                        item["image_version"],
                        run_id,
                    ),
                )
                item["assignment_id"] = int(cursor.lastrowid)
                continue
            cursor = connection.execute(
                "UPDATE cluster_assignments SET operation_run_id=? WHERE id=? AND state='active' "
                "AND revision=? AND operation_run_id IS NULL",
                (run_id, item["assignment_id"], item["expected_revision"]),
            )
            if not cursor.rowcount:
                raise RuntimeError("workload_revision_conflict")

    def active_elasticsearch_ids_for_node_in_connection(self, connection, node_id: int) -> list[int]:
        """Return active Elasticsearch workload ids affected by a host-zone change."""

        rows = connection.execute(
            "SELECT id FROM cluster_assignments WHERE node_id=? AND state='active' "
            "AND role IN ('master','hot','warm','ml','ingest','coordinating') ORDER BY id",
            (node_id,),
        ).fetchall()
        return [int(row["id"]) for row in rows]

    def has_assignments_for_member_in_connection(self, connection, cluster_id: int, node_id: int) -> bool:
        """Report whether a cluster member still owns any workload assignment."""

        return bool(
            connection.execute(
                "SELECT 1 FROM cluster_assignments WHERE cluster_id=? AND node_id=?",
                (cluster_id, node_id),
            ).fetchone()
        )

    def has_assignments_for_cluster_in_connection(self, connection, cluster_id: int) -> bool:
        return bool(
            connection.execute(
                "SELECT 1 FROM cluster_assignments WHERE cluster_id=? LIMIT 1", (cluster_id,)
            ).fetchone()
        )

    def claim_operations_in_connection(self, connection, assignment_ids: list[int], run_id: int) -> bool:
        """Atomically associate idle workloads with the supplied operation run."""

        if not assignment_ids:
            return True
        placeholders = ",".join("?" for _ in assignment_ids)
        cursor = connection.execute(
            f"UPDATE cluster_assignments SET operation_run_id=? WHERE id IN ({placeholders}) "
            "AND operation_run_id IS NULL",
            [run_id, *assignment_ids],
        )
        return cursor.rowcount == len(assignment_ids)

    def active_batch_runs(self, cluster_ids: set[int]) -> list[dict]:
        """Return active batch run projections for conflict detection."""

        if not cluster_ids:
            return []
        placeholders = ",".join("?" for _ in cluster_ids)
        with self._connection_scope() as connection:
            rows = connection.execute(
                "SELECT workload_change_batches.run_id,workload_change_batches.cluster_id "
                "FROM workload_change_batches JOIN runs ON runs.id=workload_change_batches.run_id "
                "WHERE runs.status IN ('queued','running','recovery_required') "
                f"AND workload_change_batches.cluster_id IN ({placeholders})",
                tuple(sorted(cluster_ids)),
            ).fetchall()
        return [dict(row) for row in rows]

    def batch(self, run_id: int) -> dict | None:
        """Read the encrypted controller plan owned by the workload domain."""

        with self._connection_scope() as connection:
            row = connection.execute(
                "SELECT run_id,cluster_id,plan_encrypted,completed_json,phase FROM workload_change_batches WHERE run_id=?",
                (run_id,),
            ).fetchone()
        return dict(row) if row else None

    def create_batch_in_connection(self, connection, *, run_id: int, cluster_id: int, plan_encrypted: str) -> None:
        """Persist a staged workload batch through the workload boundary."""

        connection.execute(
            "INSERT INTO workload_change_batches(run_id,cluster_id,plan_encrypted) VALUES (?,?,?)",
            (run_id, cluster_id, plan_encrypted),
        )

    def set_batch_phase(self, run_id: int, phase: str) -> None:
        if phase not in {"applying", "rolling_back"}:
            raise ValueError(f"Unsupported workload batch phase: {phase}")
        with self._connection_scope() as connection:
            connection.execute("UPDATE workload_change_batches SET phase=? WHERE run_id=?", (phase, run_id))

    def record_batch_progress(self, run_id: int, completed_client_ids: list[str]) -> None:
        with self._connection_scope() as connection:
            connection.execute(
                "UPDATE workload_change_batches SET completed_json=? WHERE run_id=?",
                (json.dumps(completed_client_ids), run_id),
            )

    def delete_batch(self, run_id: int) -> None:
        with self._connection_scope() as connection:
            connection.execute("DELETE FROM workload_change_batches WHERE run_id=?", (run_id,))

    def completed_batch_client_ids_in_connection(self, connection, run_id: int) -> set[str]:
        row = connection.execute(
            "SELECT completed_json FROM workload_change_batches WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if not row:
            return set()
        try:
            values = json.loads(row["completed_json"] or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            return set()
        return {str(value) for value in values if value is not None}

    def release_batch_in_connection(self, connection, run_id: int, changes: list[dict]) -> None:
        """Release or remove staged assignments after rollback."""

        for item in changes:
            if item["kind"] == "create":
                connection.execute(
                    "DELETE FROM cluster_assignments WHERE id=? AND operation_run_id=?",
                    (item["assignment_id"], run_id),
                )
            else:
                connection.execute(
                    "UPDATE cluster_assignments SET operation_run_id=NULL WHERE id=? AND operation_run_id=?",
                    (item["assignment_id"], run_id),
                )
        connection.execute("DELETE FROM workload_change_batches WHERE run_id=?", (run_id,))

    def recovery_batch_run_ids_in_connection(self, connection) -> list[int]:
        rows = connection.execute(
            "SELECT run_id FROM workload_change_batches"
        ).fetchall()
        return [int(row["run_id"]) for row in rows]
