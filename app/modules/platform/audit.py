"""Audit event persistence contract."""

from __future__ import annotations

from typing import Any, Callable


def write_event(db_factory: Callable, username: str, action: str, item_id: str = "", detail: str = "") -> None:
    """Append a bounded, non-secret audit record through the platform DB."""

    with db_factory() as connection:
        connection.execute(
            "INSERT INTO audit_events(username,action,item_id,detail) VALUES (?,?,?,?)",
            (username, action, item_id[:256], detail[:512]),
        )


def write_event_in_connection(
    connection: Any,
    username: str,
    action: str,
    *,
    cluster_id: int | None = None,
    item_id: str = "",
    detail: str = "",
) -> int:
    """Append an audit event while participating in an existing transaction."""

    cursor = connection.execute(
        "INSERT INTO audit_events(username,action,cluster_id,item_id,detail) VALUES(?,?,?,?,?)",
        (username, action, cluster_id, item_id[:256], detail[:512]),
    )
    return int(cursor.lastrowid)
