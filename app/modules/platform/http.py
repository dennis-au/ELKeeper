"""Platform-owned browser-facing routes.

The router is assembled with the application's persistence and token
providers, keeping the platform module independent from ``app.main`` while
preserving the existing response shapes and paths.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Callable, Mapping

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.responses import StreamingResponse

from .auth import require_user
class LoginPayload(BaseModel):
    username: str
    password: str


def build_router(
    *,
    role_specs: Mapping[str, Mapping[str, str]],
    static_dir: Path,
    run_events_token: Callable[[int], str],
    valid_run_events_token: Callable[[str, int], bool],
    token_user_fn: Callable[[str], str | None],
    password_matches: Callable[[str, str], bool],
    recent_runs: Callable[[int], list[dict]],
    stream_events: Callable[[int], object],
    signed_token_fn: Callable[[str], str],
) -> APIRouter:
    router = APIRouter()

    @router.get("/")
    async def index():
        return RedirectResponse("/dashboard")

    @router.get("/api/health")
    async def health():
        return {"status": "ok", "roles": [{"id": key, "label": value["label"]} for key, value in role_specs.items()]}

    @router.post("/api/auth/login")
    async def login(input: LoginPayload):
        if not password_matches(input.username, input.password):
            raise HTTPException(401, "Invalid username or password")
        return {"token": signed_token_fn(input.username), "username": input.username}

    @router.get("/api/runs")
    async def runs(_: Annotated[str, Depends(require_user)]):
        records = recent_runs(100)
        return [{**record, "context_json": None, "events_token": run_events_token(record["id"])} for record in records]

    @router.get("/api/runs/{run_id}/events")
    async def events(run_id: int, request: Request, token: str = ""):
        header = request.headers.get("authorization", "")
        authorized = token_user_fn(header[7:]) if header.startswith("Bearer ") else None
        if not authorized and not valid_run_events_token(token, run_id):
            raise HTTPException(401, "Authentication required")
        return StreamingResponse(stream_events(run_id), media_type="text/event-stream")

    @router.get("/{frontend_path:path}", include_in_schema=False)
    async def frontend(frontend_path: str):
        if frontend_path.startswith("api/"):
            raise HTTPException(404, "API endpoint not found")
        return FileResponse(static_dir / "index.html")

    return router
