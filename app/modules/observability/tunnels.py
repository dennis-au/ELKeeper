"""Reusable SSH and Podman tunnel runtime adapters for telemetry."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Callable


class SSHConnectionPool:
    """Keep one multiplexed SSH master per enabled host."""

    def __init__(self, *, runtime_dir: Path, ssh_args: Callable, persist_seconds: int = 120):
        self._runtime = Path(runtime_dir)
        self._ssh_args = ssh_args
        self._persist_seconds = persist_seconds
        self.sessions: dict[int, dict] = {}
        self.lock = asyncio.Lock()

    def control_path(self, node):
        return self._runtime / "ssh-control" / f"node-{node['id']}"

    def _signature(self, node):
        return (
            node["id"], node["address"], int(node["ssh_port"]), node["ssh_user"],
            tuple(self._ssh_args(node)),
        )

    async def _close_session(self, session):
        process = session.get("process")
        if process and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        session["path"].unlink(missing_ok=True)

    async def close_node(self, node_id):
        async with self.lock:
            session = self.sessions.pop(node_id, None)
            if session:
                await self._close_session(session)

    async def close(self):
        async with self.lock:
            sessions = list(self.sessions.values())
            self.sessions.clear()
            await asyncio.gather(*(self._close_session(session) for session in sessions), return_exceptions=True)

    async def _start_master(self, node):
        path = self.control_path(node)
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        path.unlink(missing_ok=True)
        base = self._ssh_args(node)
        host = base.pop()
        args = base + [
            "-o", "ControlMaster=yes", "-o", "ControlPersist=no",
            "-o", f"ControlPath={path}", "-N", host,
        ]
        process = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        for _ in range(50):
            if path.exists():
                return {"process": process, "path": path, "signature": self._signature(node)}
            if process.returncode is not None:
                error = await process.stderr.read()
                raise RuntimeError(error.decode(errors="replace").strip() or "SSH connection failed")
            await asyncio.sleep(0.1)
        await self._close_session({"process": process, "path": path})
        raise RuntimeError("SSH control connection did not become ready")

    async def ensure(self, node):
        signature = self._signature(node)
        async with self.lock:
            current = self.sessions.get(node["id"])
            if (
                current and current["signature"] == signature
                and current["process"].returncode is None and current["path"].exists()
            ):
                return current
            if current:
                await self._close_session(current)
            session = await self._start_master(node)
            self.sessions[node["id"]] = session
            return session

    def client_args(self, node):
        base = self._ssh_args(node)
        host = base.pop()
        path = self.control_path(node)
        return base + [
            "-o", "ControlMaster=auto", "-o", f"ControlPersist={self._persist_seconds}",
            "-o", f"ControlPath={path}", host,
        ]

    async def run(self, node, command, timeout=8):
        session = await self.ensure(node)
        process = await asyncio.create_subprocess_exec(
            *self.client_args(node), *command,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise RuntimeError("SSH operation timed out")
        if process.returncode:
            message = stderr.decode(errors="replace").strip() or "SSH operation failed"
            if session["process"].returncode is not None or not session["path"].exists():
                await self.close_node(node["id"])
            raise RuntimeError(message)
        return stdout


class PodmanTunnel:
    """Forward the host's rootful Podman Unix socket over SSH."""

    def __init__(self, node_id, *, runtime_dir: Path, ssh_args: Callable):
        self.node_id = node_id
        self._runtime = Path(runtime_dir)
        self._ssh_args = ssh_args
        self.path = self._runtime / f"podman-{node_id}.sock"
        self.process = None

    async def ensure(self, node):
        if self.process and self.process.returncode is None and self.path.exists():
            return self.path
        await self.close()
        self._runtime.mkdir(parents=True, exist_ok=True)
        self.path.unlink(missing_ok=True)
        args = self._ssh_args(node)[:-1] + [
            "-o", "ExitOnForwardFailure=yes", "-o", "StreamLocalBindUnlink=yes", "-NT",
            "-L", f"{self.path}:/run/podman/podman.sock", self._ssh_args(node)[-1],
        ]
        self.process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        for _ in range(50):
            if self.path.exists():
                return self.path
            if self.process.returncode is not None:
                error = await self.process.stderr.read()
                raise RuntimeError(error.decode(errors="replace").strip() or "Podman SSH tunnel failed")
            await asyncio.sleep(0.1)
        await self.close()
        raise RuntimeError("Podman SSH tunnel did not become ready")

    async def close(self):
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=2)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        self.process = None
        self.path.unlink(missing_ok=True)


__all__ = ["SSHConnectionPool", "PodmanTunnel"]
