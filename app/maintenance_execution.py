from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Mapping, Protocol

from .maintenance_lifecycle import LockScope, MaintenanceState
from .maintenance_store import (
    LockRequest,
    MaintenanceRepository,
    PlanRecord,
    RevisionConflict,
    iso_timestamp,
    parse_timestamp,
    utc_now,
)


class MaintenanceAction(str, Enum):
    EXECUTE = "execute"
    PAUSE = "pause"
    RESUME = "resume"
    CANCEL = "cancel"
    RECOVER = "recover"


class MaintenanceExecutionError(RuntimeError):
    pass


class MaintenanceValidationError(MaintenanceExecutionError):
    pass


@dataclass(frozen=True)
class AdapterResult:
    lifecycle_state: MaintenanceState | str | None = None
    stale_lock_evidence: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True)
class MaintenanceAdapterRequest:
    plan_id: str
    run_id: int
    action: MaintenanceAction
    operation_kind: str
    target_node_id: int | None
    plan_hash: str
    requested_by: str


class MaintenanceActionAdapter(Protocol):
    async def perform(self, request: MaintenanceAdapterRequest) -> AdapterResult: ...


@dataclass(frozen=True)
class MaintenanceActionTicket:
    request: MaintenanceAdapterRequest
    owner_token: str = field(repr=False)

    @property
    def plan_id(self) -> str:
        return self.request.plan_id

    @property
    def run_id(self) -> int:
        return self.request.run_id

    @property
    def action(self) -> MaintenanceAction:
        return self.request.action


# Runtime adapters are intentionally absent until their Phase 2 gate passes.
MAINTENANCE_ADAPTERS: dict[str, MaintenanceActionAdapter] = {}


_ACTION_STATES = {
    MaintenanceAction.EXECUTE: frozenset({MaintenanceState.READY}),
    MaintenanceAction.PAUSE: frozenset({MaintenanceState.EXECUTING}),
    MaintenanceAction.RESUME: frozenset({MaintenanceState.PAUSED}),
    MaintenanceAction.CANCEL: frozenset({
        MaintenanceState.DRAFT,
        MaintenanceState.READY,
        MaintenanceState.BLOCKED,
        MaintenanceState.EXECUTING,
        MaintenanceState.PAUSED,
        MaintenanceState.RECOVERY_REQUIRED,
    }),
    MaintenanceAction.RECOVER: frozenset({MaintenanceState.RECOVERY_REQUIRED}),
}

_SAFE_ACTION_CHECKPOINTS = frozenset({
    "reboot.clusters-prepared",
    "reboot.return-discovered",
})


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise MaintenanceValidationError(f"Stored {label} is invalid")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise MaintenanceValidationError(f"Stored {label} is invalid") from error
    if parsed < 0:
        raise MaintenanceValidationError(f"Stored {label} is invalid")
    return parsed


class MaintenanceExecutionService:
    """Persisted action coordinator; remote behavior exists only in injected adapters."""

    def __init__(
        self,
        repository: MaintenanceRepository,
        *,
        capability_revision: Callable[[], str],
        clock: Callable[[], datetime] = utc_now,
        lock_ttl_seconds: int = 300,
    ):
        self.repository = repository
        self.capability_revision = capability_revision
        self.clock = clock
        self.lock_ttl_seconds = lock_ttl_seconds

    def prepare(
        self,
        plan_id: str,
        action: MaintenanceAction | str,
        *,
        username: str,
    ) -> MaintenanceActionTicket:
        action = MaintenanceAction(action)
        plan = self.repository.get_plan(plan_id)
        now = self._now()
        self._validate_lifecycle(plan, action)
        self._validate_hash(plan)
        self._validate_safe_checkpoint(plan, action)
        if action in {MaintenanceAction.EXECUTE, MaintenanceAction.RESUME}:
            self._validate_execution_snapshot(plan, now)
            self._validate_current_revisions(plan)
            self._validate_no_conflicts(plan)

        active_locks = self.repository.list_active_locks(plan.id)
        stale_locks = [item for item in active_locks if item.expired(now)]
        if stale_locks and action != MaintenanceAction.RECOVER:
            raise MaintenanceValidationError(
                "Expired maintenance locks require recovery and rediscovery before this action"
            )

        run_id = self._attach_run(plan, action)
        owner_token = active_locks[0].owner_token if active_locks else ""
        if action in {MaintenanceAction.EXECUTE, MaintenanceAction.RECOVER}:
            if not stale_locks:
                requests = self._lock_requests(plan, include_recovery=action == MaintenanceAction.RECOVER)
                acquired = self.repository.acquire_locks(
                    requests,
                    owner_plan_id=plan.id,
                    run_id=run_id,
                    ttl_seconds=self.lock_ttl_seconds,
                    owner_token=owner_token or None,
                    now=now,
                )
                owner_token = acquired[0].owner_token
        elif plan.lifecycle_state in {
            MaintenanceState.EXECUTING,
            MaintenanceState.PAUSED,
            MaintenanceState.RECOVERY_REQUIRED,
        } and not active_locks:
            raise MaintenanceValidationError("Active maintenance execution has no ownership locks")

        current = self.repository.get_plan(plan.id)
        if action in {MaintenanceAction.EXECUTE, MaintenanceAction.RESUME}:
            current = self.repository.transition_plan(
                current.id,
                current.state_revision,
                MaintenanceState.EXECUTING,
                now=now,
            )
        self.repository.record_audit(
            username=username,
            action=f"maintenance-{action.value}-requested",
            cluster_id=current.target_cluster_id,
            item_id=current.id,
            detail={"plan_id": current.id, "run_id": run_id, "operation_kind": current.operation_kind},
        )
        return MaintenanceActionTicket(
            request=MaintenanceAdapterRequest(
                plan_id=current.id,
                run_id=run_id,
                action=action,
                operation_kind=current.operation_kind,
                target_node_id=current.target_node_id,
                plan_hash=current.plan_hash,
                requested_by=username,
            ),
            owner_token=owner_token,
        )

    def finalize(
        self,
        ticket: MaintenanceActionTicket,
        result: AdapterResult | None = None,
    ) -> PlanRecord:
        result = result or AdapterResult()
        plan = self.repository.get_plan(ticket.plan_id)
        self._validate_ticket(plan, ticket)
        now = self._now()
        if ticket.action == MaintenanceAction.RECOVER:
            self._recover_stale_locks(plan, ticket, result, now)
            plan = self.repository.get_plan(ticket.plan_id)

        target = self._result_state(ticket.action, result)
        if target is not None and target != plan.lifecycle_state:
            plan = self._transition_for_result(plan, target, now)

        if plan.lifecycle_state in {
            MaintenanceState.SUCCEEDED,
            MaintenanceState.FAILED,
            MaintenanceState.CANCELLED,
        }:
            self._release_active_locks(plan, ticket, reason=plan.lifecycle_state.value, now=now)
        self._update_run(ticket.run_id, plan.lifecycle_state)
        self.repository.record_audit(
            username=ticket.request.requested_by,
            action=f"maintenance-{ticket.action.value}-accepted",
            cluster_id=plan.target_cluster_id,
            item_id=plan.id,
            detail={
                "plan_id": plan.id,
                "run_id": ticket.run_id,
                "lifecycle_state": plan.lifecycle_state.value,
            },
        )
        return self.repository.get_plan(plan.id)

    def fail(self, ticket: MaintenanceActionTicket, *, error_category: str) -> PlanRecord:
        plan = self.repository.get_plan(ticket.plan_id)
        self._validate_ticket(plan, ticket, allow_hash_only=True)
        now = self._now()
        if plan.lifecycle_state in {MaintenanceState.EXECUTING, MaintenanceState.PAUSED}:
            plan = self.repository.transition_plan(
                plan.id,
                plan.state_revision,
                MaintenanceState.RECOVERY_REQUIRED,
                now=now,
            )
        elif plan.lifecycle_state == MaintenanceState.READY:
            plan = self.repository.transition_plan(
                plan.id,
                plan.state_revision,
                MaintenanceState.BLOCKED,
                now=now,
            )
        self.repository.connection.execute(
            "UPDATE runs SET status='recovery_required',finished_at=?,log=log || ? WHERE id=?",
            (
                iso_timestamp(now),
                f"Maintenance adapter stopped at a recovery boundary ({self._safe_error_category(error_category)}).\n",
                ticket.run_id,
            ),
        )
        self.repository.record_audit(
            username=ticket.request.requested_by,
            action="maintenance-adapter-recovery-required",
            cluster_id=plan.target_cluster_id,
            item_id=plan.id,
            detail={
                "plan_id": plan.id,
                "run_id": ticket.run_id,
                "error_category": self._safe_error_category(error_category),
            },
        )
        return self.repository.get_plan(plan.id)

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Maintenance execution clock must include a timezone")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _safe_error_category(value: str) -> str:
        normalized = re.sub(r"[^A-Za-z0-9_.:-]+", "-", str(value)).strip("-")
        return (normalized or "adapter-failed")[:96]

    def _validate_lifecycle(self, plan: PlanRecord, action: MaintenanceAction) -> None:
        if plan.lifecycle_state not in _ACTION_STATES[action]:
            raise MaintenanceValidationError(
                f"Maintenance {action.value} is not valid while the plan is {plan.lifecycle_state.value}"
            )

    def _validate_hash(self, plan: PlanRecord) -> None:
        if not self.repository.verify_plan_hash(plan.id, plan.plan_hash):
            raise MaintenanceValidationError("Maintenance plan hash verification failed")

    def _validate_execution_snapshot(self, plan: PlanRecord, now: datetime) -> None:
        if parse_timestamp(plan.expires_at) <= now:
            raise MaintenanceValidationError("Maintenance plan expired and must be planned again")
        observation = plan.observation if isinstance(plan.observation, Mapping) else {}
        expected_capability = observation.get("capability_revision")
        if expected_capability != self.capability_revision():
            raise MaintenanceValidationError("Maintenance capability revision changed; plan again")
        observed_at = plan.observed_at or observation.get("captured_at")
        if not isinstance(observed_at, str):
            raise MaintenanceValidationError("Maintenance plan has no fresh observation timestamp")
        try:
            captured = parse_timestamp(observed_at)
        except (TypeError, ValueError) as error:
            raise MaintenanceValidationError("Maintenance observation timestamp is invalid") from error
        policy = plan.plan.get("policy", {}) if isinstance(plan.plan, Mapping) else {}
        max_age = _integer(
            policy.get("observation_max_age_seconds", 120) if isinstance(policy, Mapping) else 120,
            "observation maximum age",
        )
        if (now - captured).total_seconds() > max_age:
            raise MaintenanceValidationError("Maintenance observations are stale; plan again")
        sources = observation.get("sources", ())
        if isinstance(sources, (list, tuple)):
            unavailable = [
                item.get("source", "unknown")
                for item in sources
                if isinstance(item, Mapping)
                and item.get("required", True)
                and item.get("status") != "ok"
            ]
            if unavailable:
                raise MaintenanceValidationError("Required maintenance observations are not healthy")

    def _validate_current_revisions(self, plan: PlanRecord) -> None:
        manifest = plan.target_manifest if isinstance(plan.target_manifest, Mapping) else {}
        for item in manifest.get("assignment_revisions", ()):
            if not isinstance(item, Mapping):
                raise MaintenanceValidationError("Stored assignment revision manifest is invalid")
            assignment_id = _integer(item.get("assignment_id"), "assignment identifier")
            expected = _integer(item.get("revision"), "assignment revision")
            row = self.repository.connection.execute(
                "SELECT revision FROM cluster_assignments WHERE id=?", (assignment_id,),
            ).fetchone()
            if row is None or row["revision"] != expected:
                raise MaintenanceValidationError(
                    f"Assignment revision changed for assignment {assignment_id}; plan again"
                )
        for item in manifest.get("policy_revisions", ()):
            if not isinstance(item, Mapping):
                raise MaintenanceValidationError("Stored policy revision manifest is invalid")
            cluster_id = _integer(item.get("cluster_id"), "cluster identifier")
            expected = _integer(item.get("revision"), "policy revision")
            policy = self.repository.get_policy(cluster_id)
            current = policy.revision if policy else 0
            if current != expected:
                raise MaintenanceValidationError(
                    f"Maintenance policy revision changed for cluster {cluster_id}; plan again"
                )
        if plan.target_node_id is not None:
            row = self.repository.connection.execute(
                "SELECT enabled FROM nodes WHERE id=?", (plan.target_node_id,),
            ).fetchone()
            if row is None or not bool(row["enabled"] if "enabled" in row.keys() else True):
                raise MaintenanceValidationError("Target host is no longer enabled")

    def _validate_no_conflicts(self, plan: PlanRecord) -> None:
        if plan.target_node_id is None:
            return
        conflicts = self.repository.observe_conflicts(
            plan.target_node_id,
            exclude_plan_id=plan.id,
            exclude_run_id=plan.run_id,
        )
        if conflicts.has_conflicts:
            raise MaintenanceValidationError("A conflicting operation now covers this maintenance target")

    def _validate_safe_checkpoint(self, plan: PlanRecord, action: MaintenanceAction) -> None:
        if action not in {MaintenanceAction.PAUSE, MaintenanceAction.RESUME, MaintenanceAction.CANCEL}:
            return
        latest = self.repository.latest_checkpoint(plan.id)
        if action == MaintenanceAction.CANCEL:
            safe = latest is None or latest.checkpoint_key == "reboot.clusters-prepared"
        else:
            safe = latest is None or latest.checkpoint_key in _SAFE_ACTION_CHECKPOINTS
        if not safe:
            raise MaintenanceValidationError(
                f"Maintenance {action.value} is available only at a safe checkpoint"
            )

    def _attach_run(self, plan: PlanRecord, action: MaintenanceAction) -> int:
        if plan.run_id is not None:
            row = self.repository.connection.execute(
                "SELECT status FROM runs WHERE id=?", (plan.run_id,),
            ).fetchone()
            if row is None:
                raise MaintenanceValidationError("Maintenance plan references a missing run")
            if row["status"] in {"succeeded", "failed", "cancelled"}:
                raise MaintenanceValidationError("Maintenance run is already complete")
            self.repository.connection.execute(
                "UPDATE runs SET status='running',finished_at=NULL WHERE id=?", (plan.run_id,),
            )
            return plan.run_id
        target = f"plan:{plan.id}"
        if plan.target_node_id is not None:
            row = self.repository.connection.execute(
                "SELECT name FROM nodes WHERE id=?", (plan.target_node_id,),
            ).fetchone()
            if row:
                target = row["name"]
        context = json.dumps(
            {"maintenance_plan_id": plan.id, "operation_kind": plan.operation_kind, "action": action.value},
            sort_keys=True,
            separators=(",", ":"),
        )
        cursor = self.repository.connection.execute(
            "INSERT INTO runs(kind,target,status,command_json,context_json) VALUES(?,?,'running','[]',?)",
            (f"maintenance-{plan.operation_kind}", target, context),
        )
        run_id = cursor.lastrowid
        result = self.repository.connection.execute(
            "UPDATE maintenance_plans SET run_id=? WHERE id=? AND run_id IS NULL",
            (run_id, plan.id),
        )
        if result.rowcount != 1:
            raise RevisionConflict("Maintenance run was attached concurrently")
        return run_id

    def _lock_requests(self, plan: PlanRecord, *, include_recovery: bool = False) -> list[LockRequest]:
        manifest = plan.target_manifest if isinstance(plan.target_manifest, Mapping) else {}
        requests = []
        if plan.target_node_id is not None:
            requests.append(LockRequest(LockScope.HOST, plan.target_node_id))
        for cluster_id in manifest.get("affected_cluster_ids", ()):
            requests.append(LockRequest(LockScope.CLUSTER, _integer(cluster_id, "cluster identifier")))
        for item in manifest.get("assignment_revisions", ()):
            if isinstance(item, Mapping):
                requests.append(LockRequest(
                    LockScope.ASSIGNMENT,
                    _integer(item.get("assignment_id"), "assignment identifier"),
                ))
        if include_recovery:
            requests.append(LockRequest(LockScope.RECOVERY, plan.id))
        if not requests:
            raise MaintenanceValidationError("Maintenance plan has no lockable target")
        return requests

    def _result_state(
        self,
        action: MaintenanceAction,
        result: AdapterResult,
    ) -> MaintenanceState | None:
        default = {
            MaintenanceAction.EXECUTE: None,
            MaintenanceAction.PAUSE: MaintenanceState.PAUSED,
            MaintenanceAction.RESUME: None,
            MaintenanceAction.CANCEL: MaintenanceState.CANCELLED,
            MaintenanceAction.RECOVER: MaintenanceState.EXECUTING,
        }[action]
        target = MaintenanceState(result.lifecycle_state) if result.lifecycle_state is not None else default
        allowed = {
            MaintenanceAction.EXECUTE: {
                None, MaintenanceState.EXECUTING, MaintenanceState.SUCCEEDED,
                MaintenanceState.FAILED, MaintenanceState.RECOVERY_REQUIRED,
            },
            MaintenanceAction.PAUSE: {MaintenanceState.PAUSED, MaintenanceState.RECOVERY_REQUIRED},
            MaintenanceAction.RESUME: {
                None, MaintenanceState.EXECUTING, MaintenanceState.SUCCEEDED,
                MaintenanceState.FAILED, MaintenanceState.RECOVERY_REQUIRED,
            },
            MaintenanceAction.CANCEL: {MaintenanceState.CANCELLED, MaintenanceState.RECOVERY_REQUIRED},
            MaintenanceAction.RECOVER: {
                MaintenanceState.EXECUTING, MaintenanceState.SUCCEEDED,
                MaintenanceState.FAILED, MaintenanceState.CANCELLED,
                MaintenanceState.RECOVERY_REQUIRED,
            },
        }[action]
        if target not in allowed:
            raise MaintenanceValidationError("Maintenance adapter returned an invalid lifecycle state")
        return target

    def _transition_for_result(
        self,
        plan: PlanRecord,
        target: MaintenanceState,
        now: datetime,
    ) -> PlanRecord:
        if target == MaintenanceState.CANCELLED and plan.lifecycle_state == MaintenanceState.EXECUTING:
            plan = self.repository.transition_plan(
                plan.id, plan.state_revision, MaintenanceState.PAUSED, now=now,
            )
        return self.repository.transition_plan(plan.id, plan.state_revision, target, now=now)

    def _recover_stale_locks(
        self,
        plan: PlanRecord,
        ticket: MaintenanceActionTicket,
        result: AdapterResult,
        now: datetime,
    ) -> None:
        active = self.repository.list_active_locks(plan.id)
        stale = [item for item in active if item.expired(now)]
        missing = [item.id for item in stale if not result.stale_lock_evidence.get(str(item.id))]
        if missing:
            raise MaintenanceValidationError(
                "Stale lock recovery requires rediscovery evidence for every expired lock"
            )
        for lock in stale:
            self.repository.recover_stale_lock(
                lock.id,
                observation=result.stale_lock_evidence[str(lock.id)],
                recovered_by=ticket.request.requested_by,
                reason="maintenance-recovery-rediscovered",
                now=now,
            )
        if stale:
            remaining = self.repository.list_active_locks(plan.id)
            token = remaining[0].owner_token if remaining else None
            self.repository.acquire_locks(
                self._lock_requests(plan, include_recovery=True),
                owner_plan_id=plan.id,
                run_id=ticket.run_id,
                ttl_seconds=self.lock_ttl_seconds,
                owner_token=token,
                now=now,
            )

    def _release_active_locks(
        self,
        plan: PlanRecord,
        ticket: MaintenanceActionTicket,
        *,
        reason: str,
        now: datetime,
    ) -> None:
        active = self.repository.list_active_locks(plan.id)
        if not active:
            return
        if any(item.expired(now) for item in active):
            raise MaintenanceValidationError(
                "Expired maintenance locks require rediscovery evidence before release"
            )
        self.repository.release_locks(
            active[0].owner_token,
            reason=reason,
            observation={"plan_id": plan.id, "run_id": ticket.run_id, "verified": True},
            now=now,
        )

    def _update_run(self, run_id: int, state: MaintenanceState) -> None:
        status = {
            MaintenanceState.SUCCEEDED: "succeeded",
            MaintenanceState.FAILED: "failed",
            MaintenanceState.CANCELLED: "cancelled",
            MaintenanceState.RECOVERY_REQUIRED: "recovery_required",
        }.get(state, "running")
        finished_at = iso_timestamp(self._now()) if status in {"succeeded", "failed", "cancelled"} else None
        self.repository.connection.execute(
            "UPDATE runs SET status=?,finished_at=? WHERE id=?",
            (status, finished_at, run_id),
        )

    def _validate_ticket(
        self,
        plan: PlanRecord,
        ticket: MaintenanceActionTicket,
        *,
        allow_hash_only: bool = False,
    ) -> None:
        if plan.run_id != ticket.run_id or plan.plan_hash != ticket.request.plan_hash:
            raise MaintenanceValidationError("Maintenance action ticket no longer matches the stored plan")
        self._validate_hash(plan)
        if not allow_hash_only and plan.operation_kind != ticket.request.operation_kind:
            raise MaintenanceValidationError("Maintenance operation kind changed")
