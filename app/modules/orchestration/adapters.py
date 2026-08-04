"""Provider-neutral orchestration adapter contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
import os
from pathlib import Path
import re
import ssl
import tempfile
from typing import Any, Mapping, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen
from urllib.parse import urlparse

from .contracts import CommandSpec, ExecutionReceipt, ExecutionStatus
from .service import LocalCommandGateway


@dataclass(frozen=True)
class SshRequest:
    address: str
    user: str
    command: tuple[str, ...]
    port: int = 22
    host_key_file: str | None = None

    def __post_init__(self) -> None:
        try:
            ipaddress.ip_address(self.address)
        except ValueError as error:
            raise ValueError("SSH addresses must be literal IPv4 or IPv6 addresses") from error
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}", self.user):
            raise ValueError("SSH user is invalid")
        if not 1 <= self.port <= 65535:
            raise ValueError("SSH port must be between 1 and 65535")
        if not self.command:
            raise ValueError("SSH command must not be empty")


class SshGateway(Protocol):
    def run(self, request: SshRequest, *, timeout: float | None = None) -> ExecutionReceipt: ...


@dataclass(frozen=True)
class PodmanRequest:
    host: str
    operation: str
    workload: str
    arguments: tuple[str, ...] = ()


class PodmanGateway(Protocol):
    def execute(self, request: PodmanRequest, *, timeout: float | None = None) -> ExecutionReceipt: ...


@dataclass(frozen=True)
class ElasticsearchRequest:
    endpoint: str
    method: str
    path: str
    payload: Mapping[str, Any] | None = None
    ca_path: str | None = None

    def __post_init__(self) -> None:
        parsed = urlparse(self.endpoint)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("Elasticsearch gateway requests require an HTTPS endpoint")
        if not self.ca_path:
            raise ValueError("Elasticsearch gateway requests require a CA path")


class ElasticsearchGateway(Protocol):
    def request(self, request: ElasticsearchRequest, *, timeout: float | None = None) -> ExecutionReceipt: ...


@dataclass(frozen=True)
class RemoteFileRequest:
    host: str
    path: str
    content: bytes | None = None
    mode: int = 0o600

    def __post_init__(self) -> None:
        if not self.path.startswith("/"):
            raise ValueError("Remote file paths must be absolute")


class RemoteFileGateway(Protocol):
    def put(self, request: RemoteFileRequest, *, timeout: float | None = None) -> ExecutionReceipt: ...

    def get(self, host: str, path: str, *, timeout: float | None = None) -> ExecutionReceipt: ...


def _executor_or_default(executor):
    return executor or LocalCommandGateway().execute


class SubprocessSshGateway:
    """Concrete SSH adapter using argv-only execution and host-key pinning."""

    def __init__(self, executor=None):
        self._execute = _executor_or_default(executor)

    def run(self, request: SshRequest, *, timeout: float | None = None) -> ExecutionReceipt:
        argv = ["ssh", "-p", str(request.port), "-o", "BatchMode=yes"]
        if request.host_key_file:
            argv.extend([
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                f"UserKnownHostsFile={request.host_key_file}",
            ])
        argv.extend([f"{request.user}@{request.address}", *request.command])
        return self._execute(CommandSpec.from_argv(argv), timeout=timeout)


class SubprocessPodmanGateway:
    """Concrete rootful Podman adapter over the SSH transport.

    It deliberately uses Podman remote SSH URLs and never creates a Podman
    TCP listener. The operation and workload remain separate argv elements.
    """

    def __init__(self, executor=None):
        self._execute = _executor_or_default(executor)

    def execute(self, request: PodmanRequest, *, timeout: float | None = None) -> ExecutionReceipt:
        if not request.host or any(value.startswith("-") for value in (request.operation, request.workload)):
            return ExecutionReceipt(status=ExecutionStatus.FAILED, detail={"error": "invalid podman request"})
        argv = [
            "podman",
            "--remote",
            "--url",
            f"ssh://{request.host}/run/podman/podman.sock",
            request.operation,
            request.workload,
            *request.arguments,
        ]
        return self._execute(CommandSpec.from_argv(argv), timeout=timeout)


class UrllibElasticsearchGateway:
    """CA-verified Elasticsearch HTTPS adapter with injectable transport."""

    def __init__(self, opener=None):
        self._opener = opener or urlopen

    def request(self, request: ElasticsearchRequest, *, timeout: float | None = None) -> ExecutionReceipt:
        path = request.path if request.path.startswith("/") else "/" + request.path
        url = urljoin(request.endpoint.rstrip("/") + "/", path.lstrip("/"))
        body = None
        headers = {"Accept": "application/json"}
        if request.payload is not None:
            import json

            body = json.dumps(dict(request.payload), sort_keys=True).encode("utf-8")
            headers["Content-Type"] = "application/json"
        http_request = Request(url, data=body, headers=headers, method=request.method.upper())
        try:
            context = ssl.create_default_context(cafile=request.ca_path)
            response = self._opener(http_request, timeout=timeout, context=context)
            with response:
                value = response.read().decode("utf-8", errors="replace")
                return ExecutionReceipt(
                    status=ExecutionStatus.SUCCEEDED,
                    returncode=getattr(response, "status", 200),
                    stdout=value,
                )
        except HTTPError as error:
            try:
                payload = error.read().decode("utf-8", errors="replace")
            except OSError:
                payload = ""
            return ExecutionReceipt(
                status=ExecutionStatus.FAILED,
                returncode=error.code,
                stderr=payload,
                detail={"status": error.code},
            )
        except (URLError, OSError, ssl.SSLError) as error:
            return ExecutionReceipt(status=ExecutionStatus.FAILED, detail={"error": type(error).__name__})


class ScpRemoteFileGateway:
    """Remote-file adapter using temporary 0600 files and SCP."""

    def __init__(self, *, user: str = "root", port: int = 22, identity_file: str | None = None, executor=None):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}", user):
            raise ValueError("SCP user is invalid")
        if not 1 <= port <= 65535:
            raise ValueError("SCP port is invalid")
        self._user = user
        self._port = port
        self._identity_file = identity_file
        self._execute = _executor_or_default(executor)

    def put(self, request: RemoteFileRequest, *, timeout: float | None = None) -> ExecutionReceipt:
        if request.content is None:
            return ExecutionReceipt(status=ExecutionStatus.FAILED, detail={"error": "file content is required"})
        if not 0 <= request.mode <= 0o777:
            return ExecutionReceipt(status=ExecutionStatus.FAILED, detail={"error": "invalid file mode"})
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(prefix="ecp-upload-", delete=False) as handle:
                temporary = Path(handle.name)
                os.chmod(temporary, 0o600)
                handle.write(request.content)
            argv = ["scp", "-P", str(self._port)]
            if self._identity_file:
                argv.extend(["-i", self._identity_file])
            argv.extend([str(temporary), f"{self._user}@{request.host}:{request.path}"])
            receipt = self._execute(CommandSpec.from_argv(argv, (temporary,)), timeout=timeout)
            if not receipt.succeeded:
                return receipt
            chmod_argv = ["ssh", "-p", str(self._port), "-o", "BatchMode=yes"]
            if self._identity_file:
                chmod_argv.extend(["-i", self._identity_file])
            chmod_argv.extend([
                f"{self._user}@{request.host}",
                "chmod",
                format(request.mode, "o"),
                "--",
                request.path,
            ])
            return self._execute(CommandSpec.from_argv(chmod_argv), timeout=timeout)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def get(self, host: str, path: str, *, timeout: float | None = None) -> ExecutionReceipt:
        request = RemoteFileRequest(host, path)
        argv = ["ssh", "-p", str(self._port), "-o", "BatchMode=yes"]
        if self._identity_file:
            argv.extend(["-i", self._identity_file])
        argv.extend([f"{self._user}@{request.host}", "cat", "--", request.path])
        return self._execute(CommandSpec.from_argv(argv), timeout=timeout)
