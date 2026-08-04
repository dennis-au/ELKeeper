"""Application lifecycle contract used by the FastAPI assembly."""

from __future__ import annotations

from contextlib import asynccontextmanager
import inspect
from typing import Any, AsyncIterator, Callable

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles


async def _invoke(callback: Callable[[], Any]) -> Any:
    result = callback()
    if inspect.isawaitable(result):
        return await result
    return result


@asynccontextmanager
async def lifecycle_context(
    initialize: Callable[[], Any],
    start: Callable[[], Any],
    recover: Callable[[], Any],
    stop: Callable[[], Any],
) -> AsyncIterator[None]:
    """Run startup and shutdown callbacks in one tested lifecycle boundary."""

    await _invoke(initialize)
    await _invoke(start)
    await _invoke(recover)
    try:
        yield
    finally:
            await _invoke(stop)


def build_lifespan(
    initialize: Callable[[], Any],
    start: Callable[[], Any],
    recover: Callable[[], Any],
    stop: Callable[[], Any],
) -> Callable:
    """Create a FastAPI lifespan callback from platform lifecycle hooks."""

    @asynccontextmanager
    async def lifespan(_application: FastAPI) -> AsyncIterator[None]:
        async with lifecycle_context(initialize, start, recover, stop):
            yield

    return lifespan


def mount_static_assets(application: FastAPI, static_dir: Any) -> None:
    """Register the compiled frontend asset mount at the platform boundary."""

    application.mount("/assets", StaticFiles(directory=static_dir / "assets", check_dir=False), name="assets")


def install_security_headers(application: FastAPI) -> None:
    """Install the controller's browser-facing baseline response headers."""

    @application.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; font-src 'self' data:; connect-src 'self'; "
            "base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
        )
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response
