from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.modules.platform.app import build_lifespan, lifecycle_context
from app.modules.platform.auth import signed_token
from app.modules.platform.http import build_router


class PlatformAppLifecycleTests(unittest.TestCase):
    def test_lifecycle_orders_sync_async_callbacks_and_stops_on_exit(self):
        calls = []

        async def start():
            calls.append("start")

        async def recover():
            calls.append("recover")

        async def stop():
            calls.append("stop")

        async def exercise():
            async with lifecycle_context(lambda: calls.append("init"), start, recover, stop):
                calls.append("run")

        asyncio.run(exercise())
        self.assertEqual(calls, ["init", "start", "recover", "run", "stop"])


class PlatformHttpRouterTests(unittest.TestCase):
    def test_login_and_run_routes_use_injected_platform_projections(self):
        calls = {"password": [], "runs": []}

        async def stream_events(_run_id):
            yield "event: completed\ndata: {\"status\":\"succeeded\"}\n\n"

        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"APP_SECRET_KEY": "platform-http-test"}):
            static_dir = Path(temporary)
            (static_dir / "assets").mkdir()
            (static_dir / "index.html").write_text('<div id="root"></div>', encoding="utf-8")
            app = FastAPI()
            app.include_router(
                build_router(
                    role_specs={"master": {"label": "Master"}},
                    static_dir=static_dir,
                    run_events_token=lambda run_id: f"run-{run_id}",
                    valid_run_events_token=lambda token, run_id: token == f"run-{run_id}",
                    token_user_fn=lambda token: "operator" if token else None,
                    password_matches=lambda username, password: calls["password"].append((username, password)) or password == "correct",
                    recent_runs=lambda limit: calls["runs"].append(limit) or [{"id": 7, "kind": "probe", "context_json": "secret"}],
                    stream_events=stream_events,
                    signed_token_fn=signed_token,
                )
            )
            with TestClient(app) as client:
                rejected = client.post("/api/auth/login", json={"username": "operator", "password": "wrong"})
                accepted = client.post("/api/auth/login", json={"username": "operator", "password": "correct"})
                token = accepted.json()["token"]
                listed = client.get("/api/runs", headers={"Authorization": f"Bearer {token}"})
                streamed = client.get("/api/runs/7/events", params={"token": "run-7"})

        self.assertEqual(rejected.status_code, 401)
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()[0]["context_json"], None)
        self.assertEqual(streamed.status_code, 200)
        self.assertIn("event: completed", streamed.text)
        self.assertEqual(calls["password"], [("operator", "wrong"), ("operator", "correct")])
        self.assertEqual(calls["runs"], [100])

    def test_platform_http_has_no_direct_sql(self):
        source = (Path(__file__).resolve().parents[1] / "app" / "modules" / "platform" / "http.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn(".execute(", source)

    def test_shutdown_is_not_entered_when_startup_fails(self):
        calls = []

        async def start():
            calls.append("start")

        async def recover():
            calls.append("recover")
            raise RuntimeError("startup failed")

        async def stop():
            calls.append("stop")

        async def exercise():
            async with lifecycle_context(lambda: calls.append("init"), start, recover, stop):
                calls.append("run")

        with self.assertRaises(RuntimeError):
            asyncio.run(exercise())
        self.assertEqual(calls, ["init", "start", "recover"])

    def test_build_lifespan_adapts_fastapi_callback_without_changing_order(self):
        calls = []

        async def exercise():
            lifespan = build_lifespan(
                lambda: calls.append("init"),
                lambda: calls.append("start"),
                lambda: calls.append("recover"),
                lambda: calls.append("stop"),
            )
            async with lifespan(object()):
                calls.append("run")

        asyncio.run(exercise())
        self.assertEqual(calls, ["init", "start", "recover", "run", "stop"])
