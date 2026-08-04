"""Typed run lifecycle contracts and compatibility persistence helpers.

The application still has a legacy worker that writes directly to the
``runs`` table.  The immutable records below define the public platform
contract for new modules without changing that worker or its API payloads.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from enum import StrEnum
from typing import Any, AsyncIterator, Callable, Iterable, Mapping, Protocol

from .security import redact


class RunState(StrEnum):
    """Stable lifecycle states shared by orchestration and maintenance."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RECOVERY_REQUIRED = "recovery_required"


class RunEventType(StrEnum):
    """Event categories persisted or streamed to an operator."""

    STARTED = "started"
    OUTPUT = "output"
    PROGRESS = "progress"
    WARNING = "warning"
    FAILED = "failed"
    COMPLETED = "completed"
    RECOVERY_REQUIRED = "recovery_required"


def _redacted_mapping(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Copy run context through the common secret redactor."""

    if value is None:
        return {}
    redacted = redact(dict(value))
    return dict(redacted) if isinstance(redacted, dict) else {}


@dataclass(frozen=True)
class RunDescriptor:
    """Operator-visible identity for a run before it receives a database id."""

    kind: str
    target: str
    context: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.kind.strip():
            raise ValueError("Run kind must not be empty")
        if not self.target.strip():
            raise ValueError("Run target must not be empty")
        object.__setattr__(self, "context", _redacted_mapping(self.context))


@dataclass(frozen=True)
class RunContext:
    """Typed handle passed between a run launcher and its worker."""

    run_id: int
    descriptor: RunDescriptor
    state: RunState = RunState.QUEUED

    def __post_init__(self) -> None:
        if self.run_id < 1:
            raise ValueError("Run id must be positive")


@dataclass(frozen=True)
class RunEvent:
    """A redacted, timestamped event suitable for persistence or SSE."""

    run_id: int
    event_type: RunEventType
    message: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        if self.run_id < 1:
            raise ValueError("Run id must be positive")
        if self.occurred_at.tzinfo is None:
            raise ValueError("Run event timestamp must include a timezone")
        object.__setattr__(self, "metadata", _redacted_mapping(self.metadata))


class RunWriter(Protocol):
    """Minimal write boundary required by new run-aware modules."""

    def append(self, event: RunEvent) -> None: ...

    def transition(self, run_id: int, state: RunState) -> None: ...


def create_run(db_factory: Callable, descriptor: RunDescriptor, command: list[str] | tuple[str, ...] = ()) -> RunContext:
    """Persist a queued run and return its typed context.

    ``command`` is expected to already be redacted by the orchestration
    contract.  This helper deliberately stores only JSON-compatible values so
    it can be adopted incrementally by existing launchers.
    """

    with db_factory() as connection:
        return create_run_in_connection(connection, descriptor, command)


def create_run_in_connection(
    connection: Any,
    descriptor: RunDescriptor,
    command: list[str] | tuple[str, ...] = (),
) -> RunContext:
    """Create a typed run inside an existing transaction."""

    cursor = connection.execute(
        "INSERT INTO runs(kind,target,status,command_json,context_json) VALUES (?,?,?, ?, ?)",
        (
            descriptor.kind,
            descriptor.target,
            RunState.QUEUED.value,
            json.dumps(list(command)),
            json.dumps(descriptor.context, sort_keys=True),
        ),
    )
    return RunContext(int(cursor.lastrowid), descriptor)


def start_run_in_connection(
    connection: Any,
    descriptor: RunDescriptor,
    command: list[str] | tuple[str, ...] = (),
) -> RunContext:
    """Create and transition a run to ``running`` in one transaction."""

    context = create_run_in_connection(connection, descriptor, command)
    transition_run_in_connection(connection, context.run_id, RunState.RUNNING)
    return RunContext(context.run_id, context.descriptor, RunState.RUNNING)


def append_event(db_factory: Callable, event: RunEvent) -> None:
    """Append one typed event to the compatibility run log."""

    prefix = f"[{event.event_type.value}] " if event.event_type is not RunEventType.OUTPUT else ""
    message = prefix + event.message
    if message and not message.endswith("\n"):
        message += "\n"
    append_log(db_factory, event.run_id, message)


def transition_run(db_factory: Callable, run_id: int, state: RunState) -> None:
    """Move a run to a known state and close terminal states."""

    with db_factory() as connection:
        transition_run_in_connection(connection, run_id, state)


def transition_run_in_connection(connection: Any, run_id: int, state: RunState) -> None:
    """Move a run inside an existing transaction."""

    terminal = state in {
        RunState.SUCCEEDED,
        RunState.FAILED,
        RunState.RECOVERY_REQUIRED,
    }
    if terminal:
        connection.execute(
            "UPDATE runs SET status=?, finished_at=CURRENT_TIMESTAMP WHERE id=?",
            (state.value, run_id),
        )
    else:
        connection.execute("UPDATE runs SET status=?, finished_at=NULL WHERE id=?", (state.value, run_id))


def update_run_status_in_connection(
    connection: Any,
    run_id: int,
    status: str,
    *,
    finished_at: str | None = None,
    log_suffix: str = "",
) -> None:
    """Persist a validated run status through the platform write boundary."""

    allowed = {state.value for state in RunState} | {"cancelled"}
    if status not in allowed:
        raise ValueError(f"Unsupported run status: {status}")
    connection.execute(
        "UPDATE runs SET status=?,finished_at=?,log=log || ? WHERE id=?",
        (status, finished_at, log_suffix, run_id),
    )


def finish_run_in_connection(
    connection: Any,
    run_id: int,
    status: str,
    *,
    log_suffix: str = "",
) -> None:
    """Close a run with a database timestamp through the run owner."""

    if status not in {RunState.SUCCEEDED.value, RunState.FAILED.value, RunState.RECOVERY_REQUIRED.value, "cancelled"}:
        raise ValueError(f"Unsupported terminal run status: {status}")
    connection.execute(
        "UPDATE runs SET status=?,finished_at=CURRENT_TIMESTAMP,log=log || ? WHERE id=?",
        (status, log_suffix, run_id),
    )


def mark_recovery_required_in_connection(
    connection: Any,
    run_ids: Iterable[int],
    message: str,
) -> None:
    """Mark queued/running runs as recovery-required through the run owner.

    Maintenance recovery needs the old ``COALESCE`` behavior so a previously
    closed run is never reopened.  Keeping this SQL here preserves that
    invariant while preventing maintenance from writing the platform table.
    """

    suffix = message if message.endswith("\n") else message + "\n"
    for run_id in sorted({int(value) for value in run_ids}):
        connection.execute(
            "UPDATE runs SET status='recovery_required',"
            "finished_at=COALESCE(finished_at,CURRENT_TIMESTAMP),log=log || ? "
            "WHERE id=? AND status IN ('queued','running')",
            (suffix, run_id),
        )


def complete_run(db_factory: Callable, event: RunEvent) -> None:
    """Record a completion event and transition according to its type."""

    append_event(db_factory, event)
    state = RunState.SUCCEEDED if event.event_type is RunEventType.COMPLETED else RunState.FAILED
    if event.event_type is RunEventType.RECOVERY_REQUIRED:
        state = RunState.RECOVERY_REQUIRED
    transition_run(db_factory, event.run_id, state)


def append_log(db_factory: Callable, run_id: int, value: str) -> None:
    with db_factory() as connection:
        append_log_in_connection(connection, run_id, value)


def append_log_in_connection(connection: Any, run_id: int, value: str) -> None:
    """Append a redacted compatibility log fragment inside an active transaction."""

    connection.execute("UPDATE runs SET log=log || ? WHERE id=?", (value, run_id))


def context_and_log_in_connection(connection: Any, run_id: int) -> tuple[dict[str, Any], str]:
    """Read the legacy context/log pair without exposing a table query to workers."""

    row = connection.execute("SELECT context_json,log FROM runs WHERE id=?", (run_id,)).fetchone()
    if not row:
        raise KeyError(run_id)
    try:
        context = json.loads(row["context_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        context = {}
    return (dict(context) if isinstance(context, dict) else {}, str(row["log"] or ""))


def set_running_command_in_connection(connection: Any, run_id: int, command: list[str] | list[list[str]]) -> None:
    """Persist a redacted command and mark its run active."""

    connection.execute(
        "UPDATE runs SET status='running',finished_at=NULL,command_json=? WHERE id=?",
        (json.dumps(command), run_id),
    )


def rename_target_in_connection(connection: Any, run_id: int, target: str) -> None:
    """Update the display target after a discovered host identity is verified."""

    connection.execute("UPDATE runs SET target=? WHERE id=?", (target, run_id))


def status_in_connection(connection: Any, run_id: int) -> str | None:
    """Return the current lifecycle state for a compatibility worker."""

    row = connection.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
    return str(row["status"]) if row else None


def statuses_in_connection(connection: Any, run_ids: Iterable[int]) -> dict[int, str]:
    """Return lifecycle states for caller-owned run references in one query."""

    normalized = sorted({int(run_id) for run_id in run_ids})
    if not normalized:
        return {}
    placeholders = ",".join("?" for _ in normalized)
    rows = connection.execute(
        "SELECT id,status FROM runs WHERE id IN (" + placeholders + ")",
        normalized,
    ).fetchall()
    return {int(row["id"]): str(row["status"]) for row in rows}


def has_active_target_in_connection(connection: Any, target: str) -> bool:
    """Check queued/running/recovery runs for one logical target namespace."""

    return bool(
        connection.execute(
            "SELECT 1 FROM runs WHERE status IN ('queued','running','recovery_required') "
            "AND (target=? OR substr(target,1,length(?) + 1)=? || ':') LIMIT 1",
            (target, target, target),
        ).fetchone()
    )


def any_active_ids_in_connection(connection: Any, run_ids: list[int]) -> bool:
    """Check lifecycle state for workload-owned operation run references."""

    if not run_ids:
        return False
    placeholders = ",".join("?" for _ in run_ids)
    return bool(
        connection.execute(
            "SELECT 1 FROM runs WHERE status IN ('queued','running','recovery_required') "
            "AND id IN (" + placeholders + ") LIMIT 1",
            run_ids,
        ).fetchone()
    )


def recovery_required_ids_in_connection(connection: Any, run_ids: list[int]) -> list[int]:
    """Filter caller-owned operation IDs using the platform run lifecycle."""

    if not run_ids:
        return []
    placeholders = ",".join("?" for _ in run_ids)
    rows = connection.execute(
        "SELECT id FROM runs WHERE status='recovery_required' AND id IN (" + placeholders + ") ORDER BY id",
        run_ids,
    ).fetchall()
    return [int(row["id"]) for row in rows]


def completed_run(db_factory: Callable, kind: str, target: str, message: str, context: Mapping | None = None) -> int:
    with db_factory() as connection:
        cursor = connection.execute(
            "INSERT INTO runs(kind,target,status,command_json,log,finished_at,context_json) VALUES (?,?, 'succeeded','[]',?,CURRENT_TIMESTAMP,?)",
            (kind, target, message.rstrip() + "\n", json.dumps(context or {})),
        )
        return int(cursor.lastrowid)


def recent_runs(db_factory: Callable, limit: int = 12) -> list[dict[str, Any]]:
    """Return bounded run summaries for observability projections."""

    bounded_limit = max(1, min(int(limit), 100))
    with db_factory() as connection:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT id,kind,target,status,created_at,finished_at "
                "FROM runs ORDER BY id DESC LIMIT ?",
                (bounded_limit,),
            )
        ]


async def stream_run_events(
    db_factory: Callable,
    run_id: int,
    *,
    poll_seconds: float = 1.0,
) -> AsyncIterator[str]:
    """Stream legacy run logs through the platform-owned SSE boundary."""

    old_log = None
    while True:
        with db_factory() as connection:
            row = connection.execute(
                "SELECT status,log FROM runs WHERE id=?", (run_id,),
            ).fetchone()
        if not row:
            yield "event: error\ndata: missing run\n\n"
            return
        if row["log"] != old_log:
            yield "event: log\ndata: " + json.dumps({"log": row["log"]}) + "\n\n"
            old_log = row["log"]
        if row["status"] in {RunState.SUCCEEDED.value, RunState.FAILED.value}:
            yield "event: completed\ndata: " + json.dumps({"status": row["status"]}) + "\n\n"
            return
        await asyncio.sleep(poll_seconds)
