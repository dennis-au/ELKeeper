"""Controller-only manual maintenance mode.

Manual mode is deliberately a small, fail-closed state machine.  It does not
run a playbook or touch a managed host: it records an operation plan, a run,
an audit event, and a host lock so other controller mutations are blocked
until the operator exits after a fresh healthy observation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from app.modules.platform import RunDescriptor, RunState, start_run_in_connection, transition_run_in_connection

from .lifecycle import HostMaintenanceState, LockScope, MaintenanceState
from .models import OperationKind
from .repository import MaintenanceRepository as MaintenanceReadRepository
from .store import (
    IdempotencyConflict,
    LockRequest,
    MaintenanceRepository,
    OverlappingPlanError,
    PlanRecord,
    RecordNotFound,
    RevisionConflict,
    StaleLockRequiresRecovery,
    iso_timestamp,
    utc_now,
)


class ManualMaintenanceError(RuntimeError):
    """Expected domain failure returned to the HTTP boundary."""


class ManualMaintenanceConflict(ManualMaintenanceError):
    """The host is already covered by another operation."""


class ManualMaintenanceRecoveryRequired(ManualMaintenanceError):
    """The requested transition cannot be proven safe."""


@dataclass(frozen=True)
class ManualHealth:
    reachable: bool
    initialized: bool
    podman_socket_active: bool
    observed_at: datetime | None
    last_error: str = ""
    source: str = "unknown"

    @property
    def healthy(self) -> bool:
        return bool(
            self.reachable
            and self.initialized
            and self.podman_socket_active
            and not self.last_error
            and self.observed_at is not None
        )


def _parse_observed_at(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def observe_manual_health(
    connection: Any,
    telemetry: Any,
    node_id: int,
    *,
    clock: Callable[[], datetime] = utc_now,
    max_age_seconds: int = 300,
) -> ManualHealth:
    """Return the newest local host observation without remote I/O."""

    state = getattr(telemetry, "host_states", {}).get(node_id) if telemetry is not None else None
    source = "telemetry" if state else "database"
    if not state:
        projection = MaintenanceReadRepository.from_connection(connection).host_runtime(node_id)
        state = dict(projection.record) if projection else {}
    observed_at = _parse_observed_at(state.get("observed_at"))
    now = clock().astimezone(timezone.utc)
    if observed_at is None or now - observed_at > timedelta(seconds=max_age_seconds):
        return ManualHealth(
            reachable=bool(state.get("reachable")),
            initialized=bool(state.get("initialized")),
            podman_socket_active=bool(state.get("podman_socket_active")),
            observed_at=observed_at,
            last_error="stale or missing host observation",
            source=source,
        )
    return ManualHealth(
        reachable=bool(state.get("reachable")),
        initialized=bool(state.get("initialized")),
        podman_socket_active=bool(state.get("podman_socket_active")),
        observed_at=observed_at,
        last_error=str(state.get("last_error") or ""),
        source=source,
    )


class ManualMaintenanceService:
    """Persist and transition manual host maintenance mode."""

    def __init__(
        self,
        repository: MaintenanceRepository,
        *,
        telemetry: Any = None,
        clock: Callable[[], datetime] = utc_now,
        max_observation_age_seconds: int = 300,
    ):
        self.repository = repository
        self.telemetry = telemetry
        self.clock = clock
        self.max_observation_age_seconds = max_observation_age_seconds

    def _active_plan(self, node_id: int) -> tuple[Any, PlanRecord] | None:
        state = self.repository.get_host_state(node_id)
        if state.state == HostMaintenanceState.AVAILABLE:
            return state, None  # type: ignore[return-value]
        if not state.active_plan_id:
            raise ManualMaintenanceRecoveryRequired("Host maintenance state has no active plan")
        return state, self.repository.get_plan(state.active_plan_id)

    def enter(
        self,
        node_id: int,
        *,
        requested_by: str,
        reason: str,
        idempotency_key: str,
        duration_seconds: int,
    ) -> dict[str, Any]:
        if not reason.strip():
            raise ValueError("reason must not be blank")
        if duration_seconds < 60 or duration_seconds > 7 * 24 * 60 * 60:
            raise ValueError("duration_seconds must be between 60 and 604800")
        host_record = MaintenanceReadRepository.from_connection(self.repository.connection).host(node_id)
        if not host_record:
            raise RecordNotFound(f"Host {node_id} was not found")
        state = self.repository.get_host_state(node_id)
        if state.state == HostMaintenanceState.MAINTENANCE and state.active_plan_id:
            plan = self.repository.get_plan(state.active_plan_id)
            return self._response(state, plan)
        if state.state != HostMaintenanceState.AVAILABLE:
            raise ManualMaintenanceConflict(f"Host is already in {state.state.value} state")
        conflicts = self.repository.observe_conflicts(node_id)
        if conflicts.has_conflicts:
            raise ManualMaintenanceConflict(
                "Host has conflicting maintenance operations: " + ", ".join(conflicts.conflict_identifiers)
            )
        now = self.clock().astimezone(timezone.utc)
        expires_at = now + timedelta(seconds=duration_seconds)
        plan_payload = {
            "operation": OperationKind.MANUAL_MAINTENANCE.value,
            "target": {"node_id": node_id, "reason": reason.strip()},
            "non_mutating": True,
            "expiry": iso_timestamp(expires_at),
        }
        manifest = {
            "affected_cluster_ids": list(conflicts.cluster_ids),
            "assignment_revisions": [],
            "manual_mode": True,
        }
        connection = self.repository.connection
        connection.execute("SAVEPOINT manual_maintenance_enter")
        try:
            plan = self.repository.create_plan(
                operation_kind=OperationKind.MANUAL_MAINTENANCE.value,
                plan=plan_payload,
                idempotency_key=idempotency_key,
                requested_by=requested_by,
                expires_at=expires_at,
                observation={"captured_at": iso_timestamp(now), "source": "controller"},
                target_node_id=node_id,
                target_manifest=manifest,
                initial_state=MaintenanceState.READY,
                retention_until=expires_at + timedelta(days=30),
            )
            plan = self.repository.transition_plan(plan.id, plan.state_revision, MaintenanceState.EXECUTING, now=now)
            run = start_run_in_connection(
                connection,
                RunDescriptor(
                    "manual-maintenance",
                    host_record.name,
                    {"maintenance_plan_id": plan.id, "operation_kind": plan.operation_kind, "node_id": node_id},
                ),
            )
            self.repository.attach_run_id(plan.id, run.run_id)
            self.repository.acquire_locks(
                [LockRequest(LockScope.HOST, node_id)],
                owner_plan_id=plan.id,
                run_id=run.run_id,
                ttl_seconds=duration_seconds,
                now=now,
            )
            planning_state = self.repository.transition_host_state(
                node_id, state.state_revision, HostMaintenanceState.PLANNING, plan.id, now=now,
            )
            active_state = self.repository.transition_host_state(
                node_id, planning_state.state_revision, HostMaintenanceState.MAINTENANCE, plan.id, now=now,
            )
            self.repository.record_audit(
                username=requested_by,
                action="manual-maintenance-entered",
                item_id=plan.id,
                detail={"node_id": node_id, "expires_at": iso_timestamp(expires_at), "remote_mutation": False},
            )
            connection.execute("RELEASE SAVEPOINT manual_maintenance_enter")
            return self._response(active_state, self.repository.get_plan(plan.id))
        except Exception:
            connection.execute("ROLLBACK TO SAVEPOINT manual_maintenance_enter")
            connection.execute("RELEASE SAVEPOINT manual_maintenance_enter")
            raise

    def exit(self, node_id: int, *, requested_by: str, reason: str = "Manual maintenance complete") -> dict[str, Any]:
        state = self.repository.get_host_state(node_id)
        if state.state == HostMaintenanceState.AVAILABLE:
            return {"node_id": node_id, "state": state.state.value, "run_id": None, "plan_id": None}
        if state.state != HostMaintenanceState.MAINTENANCE or not state.active_plan_id:
            raise ManualMaintenanceRecoveryRequired(
                f"Host is in {state.state.value}; rediscovery is required before exit"
            )
        plan = self.repository.get_plan(state.active_plan_id)
        health = observe_manual_health(
            self.repository.connection,
            self.telemetry,
            node_id,
            clock=self.clock,
            max_age_seconds=self.max_observation_age_seconds,
        )
        if not health.healthy:
            return self._record_recovery(
                state,
                plan,
                requested_by=requested_by,
                reason="Fresh healthy host observation is required before leaving manual maintenance mode",
                evidence={"health_source": health.source, "health_reason": health.last_error},
            )
        locks = self.repository.list_active_locks(plan.id)
        if not locks:
            return self._record_recovery(
                state,
                plan,
                requested_by=requested_by,
                reason="Manual maintenance lock is missing; rediscovery is required",
                evidence={"health_source": health.source, "lock": "missing"},
            )
        try:
            self.repository.release_locks(
                locks[0].owner_token,
                reason="manual-maintenance-exit",
                observation={"health_source": health.source, "observed_at": iso_timestamp(health.observed_at)},
                now=self.clock(),
            )
            available = self.repository.transition_host_state(
                node_id, state.state_revision, HostMaintenanceState.AVAILABLE, None, now=self.clock(),
            )
            completed = self.repository.transition_plan(
                plan.id, plan.state_revision, MaintenanceState.SUCCEEDED, now=self.clock(),
            )
            if plan.run_id:
                transition_run_in_connection(self.repository.connection, plan.run_id, RunState.SUCCEEDED)
            self.repository.record_audit(
                username=requested_by,
                action="manual-maintenance-exited",
                item_id=plan.id,
                detail={"node_id": node_id, "reason": reason.strip(), "observed_at": iso_timestamp(health.observed_at)},
            )
        except (RevisionConflict, StaleLockRequiresRecovery) as error:
            return self._record_recovery(
                state,
                plan,
                requested_by=requested_by,
                reason=str(error),
                evidence={"health_source": health.source, "lock": "stale-or-concurrent"},
            )
        return self._response(available, completed)

    def _record_recovery(
        self,
        state,
        plan: PlanRecord,
        *,
        requested_by: str,
        reason: str,
        evidence: Mapping[str, Any],
    ) -> dict[str, Any]:
        now = self.clock()
        recovery_state = self.repository.transition_host_state(
            state.node_id,
            state.state_revision,
            HostMaintenanceState.RECOVERY_REQUIRED,
            plan.id,
            now=now,
        )
        recovery_plan = self.repository.transition_plan(
            plan.id,
            plan.state_revision,
            MaintenanceState.RECOVERY_REQUIRED,
            now=now,
        )
        if plan.run_id:
            transition_run_in_connection(self.repository.connection, plan.run_id, RunState.RECOVERY_REQUIRED)
        self.repository.record_audit(
            username=requested_by,
            action="manual-maintenance-exit-recovery-required",
            item_id=plan.id,
            detail={"node_id": state.node_id, "reason": reason, "evidence": dict(evidence)},
        )
        return {
            **self._response(recovery_state, recovery_plan),
            "recovery_required": True,
            "recovery_reason": reason,
        }

    @staticmethod
    def _response(state, plan: PlanRecord) -> dict[str, Any]:
        return {
            "node_id": state.node_id,
            "state": state.state.value,
            "state_revision": state.state_revision,
            "plan_id": plan.id,
            "run_id": plan.run_id,
            "expires_at": plan.expires_at,
            "lifecycle_state": plan.lifecycle_state.value,
        }
