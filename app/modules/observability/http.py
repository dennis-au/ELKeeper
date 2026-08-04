"""Browser-facing dashboard and telemetry stream routes.

The telemetry manager remains owned by the observability runtime.  This
module owns only the HTTP contract and receives the application-specific
database and token providers during assembly.
"""

from __future__ import annotations

import asyncio
import json
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.modules.platform import recent_runs, require_user


def build_router(
    *,
    db_factory: Callable,
    telemetry_provider: Callable[[], object],
    signed_scope_token: Callable[[str], str],
    valid_scope_token: Callable[[str, str], bool],
    token_user: Callable[[str], str | None],
    host_provider: Callable[[int], dict | None] | None = None,
    observation_provider: Callable[[int], dict | None] | None = None,
    user_dependency: Callable = require_user,
    stream_token_ttl: int = 600,
) -> APIRouter:
    """Build dashboard routes without importing ``app.main`` or ``app.console``."""

    router = APIRouter()

    @router.get("/api/dashboard/snapshot")
    async def dashboard_snapshot(_: str = Depends(user_dependency)):
        return telemetry_provider().snapshot()

    @router.post("/api/dashboard/stream-token")
    async def dashboard_stream_token(_: str = Depends(user_dependency)):
        return {"token": signed_scope_token("dashboard"), "expires_in": stream_token_ttl}

    @router.get("/api/dashboard/events")
    async def dashboard_events(request: Request, token: str = ""):
        header = request.headers.get("authorization", "")
        authorized = token_user(header[7:]) if header.startswith("Bearer ") else None
        if not authorized and not valid_scope_token(token, "dashboard"):
            raise HTTPException(401, "Authentication required")

        async def stream():
            telemetry = telemetry_provider()
            queue = telemetry.subscribe()
            last_runs = None
            try:
                snapshot = telemetry.snapshot()
                yield "event: snapshot\ndata: " + json.dumps(snapshot) + "\n\n"
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        message = await asyncio.wait_for(queue.get(), timeout=3)
                        yield f"id: {message['id']}\nevent: {message['event']}\ndata: {json.dumps(message['data'])}\n\n"
                    except asyncio.TimeoutError:
                        runs = recent_runs(db_factory, 12)
                        encoded = json.dumps(runs, sort_keys=True)
                        if encoded != last_runs:
                            yield "event: run\ndata: " + json.dumps({"runs": runs}) + "\n\n"
                            last_runs = encoded
                        else:
                            yield ": heartbeat\n\n"
            finally:
                telemetry.unsubscribe(queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    if host_provider is not None and observation_provider is not None:
        @router.get("/api/nodes/{node_id}/runtime")
        async def node_runtime(node_id: int, _: str = Depends(user_dependency)):
            node = host_provider(node_id)
            if not node:
                raise HTTPException(404, "Host not found")
            observed = observation_provider(node_id)
            telemetry = telemetry_provider()
            state = telemetry.host_states.get(node_id) or (observed or {})
            return {
                "node_id": node_id,
                "initialized": bool(state.get("initialized", 0)),
                "reachable": bool(state.get("reachable", 0)),
                "podman_socket_active": bool(state.get("podman_socket_active", 0)),
                "os_name": state.get("os_name", ""),
                "podman_version": state.get("podman_version", ""),
                "observed_at": state.get("observed_at"),
                "last_error": state.get("last_error", ""),
                "containers": state.get("containers", []),
                "pods": state.get("pods", []),
            }

    return router


__all__ = ["build_router"]
