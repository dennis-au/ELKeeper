#!/usr/bin/env python3
"""Start an isolated controller candidate and verify its browser-facing basics."""

from __future__ import annotations

import argparse
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
import time
from urllib.error import URLError
from urllib.request import Request, urlopen


def isolated_credentials() -> dict[str, str]:
    return {
        "APP_SECRET_KEY": secrets.token_urlsafe(32),
        "ADMIN_USERNAME": "smoke-operator",
        "ADMIN_PASSWORD": secrets.token_urlsafe(24),
    }


def request(url: str, *, token: str = "", payload: dict | None = None) -> tuple[int, bytes]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    data = json.dumps(payload).encode() if payload is not None else None
    if data:
        headers["Content-Type"] = "application/json"
    try:
        with urlopen(Request(url, data=data, headers=headers), timeout=3) as response:
            return response.status, response.read()
    except URLError:
        return 0, b""


def sse_status(port: int, token: str) -> int:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    try:
        connection.request("GET", "/api/runs/1/events", headers={"Authorization": f"Bearer {token}"})
        return connection.getresponse().status
    except OSError:
        return 0
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--port", type=int, default=18083)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    credentials = isolated_credentials()
    container_env = os.environ.copy()
    container_env.update(credentials)
    name = f"ecp-smoke-{args.port}"
    base = f"http://127.0.0.1:{args.port}"
    with tempfile.TemporaryDirectory(prefix="ecp-smoke-") as temporary:
        temporary_path = Path(temporary)
        for directory in ("data", "config", "runtime"):
            (temporary_path / directory).mkdir()
        subprocess.run(["podman", "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        command = [
            "podman", "run", "-d", "--name", name,
            "-e", "APP_SECRET_KEY", "-e", "ADMIN_USERNAME", "-e", "ADMIN_PASSWORD",
            "-e", "APP_DATA_DIR=/var/lib/elastic-control", "-e", "APP_CONFIG_DIR=/config",
            "-e", "APP_RUNTIME_DIR=/run/elastic-control", "-p", f"127.0.0.1:{args.port}:8080",
            "-v", f"{temporary_path / 'data'}:/var/lib/elastic-control:Z",
            "-v", f"{temporary_path / 'config'}:/config:Z", "-v", f"{temporary_path / 'runtime'}:/run/elastic-control:Z",
            args.image,
        ]
        try:
            subprocess.run(command, stdout=subprocess.DEVNULL, check=True, env=container_env)
            for _ in range(30):
                if request(f"{base}/api/health")[0] == 200:
                    break
                time.sleep(1)
            if request(f"{base}/api/health")[0] != 200:
                raise RuntimeError("candidate health endpoint did not become ready")
            status, body = request(
                f"{base}/api/auth/login",
                payload={"username": credentials["ADMIN_USERNAME"], "password": credentials["ADMIN_PASSWORD"]},
            )
            token = json.loads(body).get("token", "") if status == 200 else ""
            if not token or request(f"{base}/api/clusters", token=token)[0] != 200 or request(f"{base}/api/runs", token=token)[0] != 200:
                raise RuntimeError("candidate authentication or authenticated API check failed")
            dashboard_status, dashboard = request(f"{base}/dashboard")
            if dashboard_status != 200 or any(request(f"{base}/{route}")[0] != 200 for route in ("clusters", "hosts", "roles", "advanced")):
                raise RuntimeError("candidate SPA route check failed")
            asset = re.search(rb'(?:src|href)="(/assets/[^"]+)"', dashboard)
            if not asset or request(f"{base}{asset.group(1).decode()}")[0] != 200:
                raise RuntimeError("candidate static asset check failed")
            if sse_status(args.port, token) != 200:
                raise RuntimeError("candidate SSE connection check failed")
            print("Isolated candidate smoke: passed")
            return 0
        finally:
            subprocess.run(["podman", "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


if __name__ == "__main__":
    raise SystemExit(main())
