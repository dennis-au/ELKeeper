from __future__ import annotations

import base64
import ipaddress
import json
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any, Protocol

from .maintenance_post_return import CleanupProof, CleanupStatus, ExecutorCleanupTarget
from .maintenance_reboot import ReconnectObservation, SshDisconnectObservation
from .maintenance_runtime import (
    ControllerMaintenanceIO,
    ExecutionOutcome,
    ExecutionReceipt,
    ManagedFileObservation,
    MaintenanceRuntimeFlags,
    PlaybookExecutionRequest,
    RebootRequestReceipt,
    RemoteOutcomeUnknown,
    RuntimeMutationDisabled,
)
from .maintenance_executor import executor_instance_unit, validate_cleanup_paths, validate_managed_unit, validate_operation_id


NODE_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
SSH_USER_PATTERN = r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$"
INVOCATION_PATTERN = r"^[A-Za-z0-9._:-]+$"
MAX_REMOTE_OUTPUT_BYTES = 262144


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone")
    return value.astimezone(timezone.utc)


class ControllerNodeRecord:
    """Small normalized inventory view used by the injected callbacks."""

    def __init__(self, *, node_id: int, name: str, address: str, ssh_port: int, ssh_user: str):
        if node_id < 1:
            raise ValueError("node_id must be positive")
        if not re.fullmatch(NODE_NAME_PATTERN, name):
            raise ValueError("inventory name is invalid")
        try:
            ipaddress.ip_address(address)
        except ValueError as error:
            raise ValueError("SSH address must be a literal IP address") from error
        if not 1 <= ssh_port <= 65535:
            raise ValueError("SSH port is invalid")
        if not re.fullmatch(SSH_USER_PATTERN, ssh_user):
            raise ValueError("SSH user is invalid")
        self.node_id = node_id
        self.name = name
        self.address = address
        self.ssh_port = ssh_port
        self.ssh_user = ssh_user

def normalize_node(value: Mapping[str, Any], *, node_id: int | None = None) -> ControllerNodeRecord:
    return ControllerNodeRecord(
        node_id=int(value.get("id", node_id or 0)),
        name=str(value.get("name", "")),
        address=str(value.get("address", "")),
        ssh_port=int(value.get("ssh_port", 22)),
        ssh_user=str(value.get("ssh_user", "root")),
    )


class SSHCommandRequest:
    def __init__(
        self,
        *,
        node: ControllerNodeRecord,
        argv: Sequence[str],
        key_path: str,
        known_hosts_path: str,
        host_key_args: Sequence[str],
        timeout_seconds: int,
    ):
        if not argv or any(not isinstance(item, str) or not item for item in argv):
            raise ValueError("SSH argv must contain non-empty strings")
        self.node = node
        self.argv = tuple(argv)
        self.key_path = _absolute_path(key_path, "SSH key path")
        self.known_hosts_path = _absolute_path(known_hosts_path, "known-hosts path")
        self.host_key_args = tuple(host_key_args)
        self.timeout_seconds = timeout_seconds

    def redacted(self) -> dict[str, Any]:
        return {
            "node_id": self.node.node_id,
            "address": self.node.address,
            "port": self.node.ssh_port,
            "user": self.node.ssh_user,
            "argv": self.argv,
            "key_path": self.key_path,
            "known_hosts_path": self.known_hosts_path,
            "host_key_args": self.host_key_args,
            "timeout_seconds": self.timeout_seconds,
        }


class SSHCommandResult:
    def __init__(self, *, return_code: int, stdout: bytes = b"", stderr: bytes = b""):
        if not isinstance(return_code, int):
            raise ValueError("SSH return code must be an integer")
        if len(stdout) > MAX_REMOTE_OUTPUT_BYTES or len(stderr) > MAX_REMOTE_OUTPUT_BYTES:
            raise ValueError("SSH output exceeds the bounded runtime limit")
        self.return_code = return_code
        self.stdout = bytes(stdout)
        self.stderr = bytes(stderr)


class SSHRunner(Protocol):
    async def run(self, request: SSHCommandRequest) -> SSHCommandResult: ...


class AnsibleInvocation:
    def __init__(
        self,
        *,
        request: PlaybookExecutionRequest,
        node: ControllerNodeRecord,
        private_key_path: str,
        known_hosts_path: str,
        host_key_args: Sequence[str],
    ):
        self.request = request
        self.node = node
        self.private_key_path = _absolute_path(private_key_path, "Ansible private-key path")
        self.known_hosts_path = _absolute_path(known_hosts_path, "Ansible known-hosts path")
        self.host_key_args = tuple(host_key_args)

    def redacted(self) -> dict[str, Any]:
        return {
            "node_id": self.node.node_id,
            "node_name": self.node.name,
            "playbook": self.request.playbook,
            "private_key_path": self.private_key_path,
            "known_hosts_path": self.known_hosts_path,
            "host_key_args": self.host_key_args,
            "variables": {key: "configured" if "key" in key.lower() else value for key, value in self.request.variables.items()},
        }


class AnsibleRunner(Protocol):
    async def run(self, invocation: AnsibleInvocation) -> ExecutionReceipt: ...


class RebootRunner(Protocol):
    async def request(self, *, node: ControllerNodeRecord, operation_id: str) -> RebootRequestReceipt: ...


class EndpointProbe(Protocol):
    async def __call__(self, *, node: ControllerNodeRecord, endpoint_ref: str) -> bool: ...


class RemoteFileObserver(Protocol):
    async def __call__(
        self, *, node: ControllerNodeRecord, path: str, maximum_bytes: int,
    ) -> ManagedFileObservation: ...


def _absolute_path(value: str, label: str) -> str:
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts or str(path) != value:
        raise ValueError(f"{label} must be an absolute normalized path")
    return value


def _managed_observation_path(value: str) -> str:
    value = _absolute_path(value, "remote file path")
    path = PurePosixPath(value)
    roots = (
        PurePosixPath("/var/lib/elastic-control/maintenance"),
        PurePosixPath("/etc/elastic-control"),
    )
    if not any(path == root or root in path.parents for root in roots):
        raise ValueError("remote file path is outside the controller-owned maintenance roots")
    return value


def _validate_host_key_policy(arguments: Sequence[str], known_hosts: str) -> tuple[str, ...]:
    values = tuple(arguments)
    if any(not isinstance(value, str) or not value for value in values):
        raise ValueError("SSH host-key arguments must contain non-empty strings")
    known_hosts_arg = f"UserKnownHostsFile={known_hosts}"
    if known_hosts_arg not in values:
        raise ValueError("SSH host-key policy must name the controller-generated known-hosts file")
    if not any(value in ("StrictHostKeyChecking=yes", "StrictHostKeyChecking=no") for value in values):
        raise ValueError("SSH host-key policy must explicitly set StrictHostKeyChecking")
    return values


def _node_mapping(node: ControllerNodeRecord) -> dict[str, Any]:
    return {
        "id": node.node_id,
        "name": node.name,
        "address": node.address,
        "ssh_port": node.ssh_port,
        "ssh_user": node.ssh_user,
    }


_FILE_PROBE_SCRIPT = r'''import base64,json,os,stat,sys
path=sys.argv[1]; maximum=int(sys.argv[2])
try:
    before=os.lstat(path)
except FileNotFoundError:
    print(json.dumps({"exists":False},separators=(",",":"))); raise SystemExit(0)
if stat.S_ISLNK(before.st_mode):
    print(json.dumps({"exists":True,"regular":False,"symlink":True,"owner_uid":before.st_uid,"mode":stat.S_IMODE(before.st_mode)},separators=(",",":"))); raise SystemExit(0)
if not stat.S_ISREG(before.st_mode) or before.st_uid != 0 or stat.S_IMODE(before.st_mode) != 0o600 or before.st_size < 1 or before.st_size > maximum:
    print(json.dumps({"exists":True,"regular":stat.S_ISREG(before.st_mode),"symlink":False,"owner_uid":before.st_uid,"mode":stat.S_IMODE(before.st_mode)},separators=(",",":"))); raise SystemExit(0)
fd=os.open(path,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0))
try:
    after=os.fstat(fd)
    if (before.st_dev,before.st_ino)!=(after.st_dev,after.st_ino): raise RuntimeError("changed")
    content=os.read(fd,maximum+1)
finally: os.close(fd)
if len(content)>maximum: raise RuntimeError("oversized")
print(json.dumps({"exists":True,"regular":True,"symlink":False,"owner_uid":after.st_uid,"mode":stat.S_IMODE(after.st_mode),"content":base64.b64encode(content).decode("ascii")},separators=(",",":")))'''

_CLEANUP_SCRIPT = r'''import os,re,stat,subprocess,sys
unit=sys.argv[1]; paths=sys.argv[2:]
if not re.fullmatch(r"ecp-maintenance-resume@[0-9a-f]{32}\.service",unit): raise SystemExit(20)
subprocess.run(["systemctl","disable",unit],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=False,timeout=30)
for value in paths:
    path=os.path.abspath(value)
    if not (path.startswith("/var/lib/elastic-control/maintenance/operations/") and path == os.path.normpath(value)): raise SystemExit(21)
    try: item=os.lstat(path)
    except FileNotFoundError: continue
    if stat.S_ISLNK(item.st_mode) or not stat.S_ISREG(item.st_mode) or item.st_uid != 0 or stat.S_IMODE(item.st_mode) != 0o600: raise SystemExit(22)
    os.unlink(path)
raise SystemExit(0)'''


class ControllerMaintenanceIOAdapter(ControllerMaintenanceIO):
    """Default-disabled concrete IO adapter for maintenance_runtime."""

    def __init__(
        self,
        *,
        node_resolver: Callable[[int], Mapping[str, Any]],
        active_key_path: Callable[[], str],
        known_hosts_path: Callable[[Sequence[int]], str],
        host_key_args: Callable[[Mapping[str, Any], str], Sequence[str]],
        ssh_runner: SSHRunner,
        ansible_runner: AnsibleRunner,
        reboot_runner: RebootRunner | None = None,
        endpoint_probes: Mapping[str, EndpointProbe] | None = None,
        remote_file_observer: RemoteFileObserver | None = None,
        flags: MaintenanceRuntimeFlags | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ):
        self.node_resolver = node_resolver
        self.active_key_path = active_key_path
        self.known_hosts_path = known_hosts_path
        self.host_key_args = host_key_args
        self.ssh_runner = ssh_runner
        self.ansible_runner = ansible_runner
        self.reboot_runner = reboot_runner
        self.endpoint_probes = dict(endpoint_probes or {})
        self.remote_file_observer = remote_file_observer
        self.flags = flags or MaintenanceRuntimeFlags()
        self.clock = clock

    def _node(self, node_id: int) -> ControllerNodeRecord:
        return normalize_node(self.node_resolver(node_id), node_id=node_id)

    def _ssh_request(self, node_id: int, argv: Sequence[str], timeout_seconds: int) -> SSHCommandRequest:
        node = self._node(node_id)
        key_path = self.active_key_path()
        known_hosts = self.known_hosts_path((node_id,))
        policy = tuple(self.host_key_args(_node_mapping(node), known_hosts))
        policy = _validate_host_key_policy(policy, known_hosts)
        return SSHCommandRequest(
            node=node,
            argv=argv,
            key_path=key_path,
            known_hosts_path=known_hosts,
            host_key_args=policy,
            timeout_seconds=timeout_seconds,
        )

    async def run_playbook(self, request: PlaybookExecutionRequest) -> ExecutionReceipt:
        if not self.flags.executor_staging_enabled:
            raise RuntimeMutationDisabled("maintenance playbook execution is disabled")
        node = self._node(request.node_id)
        key_path = self.active_key_path()
        known_hosts = self.known_hosts_path((node.node_id,))
        invocation = AnsibleInvocation(
            request=request,
            node=node,
            private_key_path=key_path,
            known_hosts_path=known_hosts,
            host_key_args=_validate_host_key_policy(
                self.host_key_args(_node_mapping(node), known_hosts), known_hosts,
            ),
        )
        return await self.ansible_runner.run(invocation)

    async def request_reboot(self, *, node_id: int, operation_id: str) -> RebootRequestReceipt:
        if not self.flags.reboot_enabled:
            raise RuntimeMutationDisabled("maintenance reboot execution is disabled")
        if self.reboot_runner is None:
            raise RuntimeMutationDisabled("no low-level reboot runner is configured")
        return await self.reboot_runner.request(
            node=self._node(node_id),
            operation_id=validate_operation_id(operation_id),
        )

    async def wait_for_disconnect(self, *, node_id: int, invocation_id: str) -> SshDisconnectObservation:
        if not re.fullmatch(INVOCATION_PATTERN, invocation_id):
            raise ValueError("reboot invocation ID is invalid")
        request = self._ssh_request(node_id, ("true",), 5)
        try:
            result = await self.ssh_runner.run(request)
        except (OSError, TimeoutError, RemoteOutcomeUnknown):
            return SshDisconnectObservation(disconnected=True, observed_at=self.clock())
        if result.return_code != 0:
            return SshDisconnectObservation(disconnected=True, observed_at=self.clock())
        return SshDisconnectObservation(disconnected=False, observed_at=self.clock())

    async def wait_for_reconnect(self, *, node_id: int) -> ReconnectObservation:
        request = self._ssh_request(node_id, ("cat", "/proc/sys/kernel/random/boot_id"), 8)
        try:
            result = await self.ssh_runner.run(request)
        except (OSError, TimeoutError, RemoteOutcomeUnknown):
            return ReconnectObservation(connected=False, boot_id=None, observed_at=self.clock())
        if result.return_code != 0:
            return ReconnectObservation(connected=False, boot_id=None, observed_at=self.clock())
        boot_id = result.stdout.decode("ascii", errors="strict").strip()
        if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", boot_id):
            return ReconnectObservation(connected=False, boot_id=None, observed_at=self.clock())
        return ReconnectObservation(connected=True, boot_id=boot_id, observed_at=self.clock())

    async def wait_for_ssh(self, *, node_id: int, timeout_seconds: int) -> bool:
        request = self._ssh_request(node_id, ("true",), min(timeout_seconds, 30))
        try:
            result = await self.ssh_runner.run(request)
        except (OSError, TimeoutError, RemoteOutcomeUnknown):
            return False
        return result.return_code == 0

    async def read_boot_id(self, *, node_id: int) -> str | None:
        request = self._ssh_request(node_id, ("cat", "/proc/sys/kernel/random/boot_id"), 8)
        try:
            result = await self.ssh_runner.run(request)
            value = result.stdout.decode("ascii", errors="strict").strip()
        except (OSError, TimeoutError, RemoteOutcomeUnknown, UnicodeError):
            return None
        if result.return_code or not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", value):
            return None
        return value

    async def observe_file(self, *, node_id: int, path: str, maximum_bytes: int) -> ManagedFileObservation:
        _managed_observation_path(path)
        if maximum_bytes < 1 or maximum_bytes > 262144:
            raise ValueError("remote file size limit is invalid")
        if self.remote_file_observer is not None:
            observation = await self.remote_file_observer(
                node=self._node(node_id), path=path, maximum_bytes=maximum_bytes,
            )
            if observation.path != path:
                raise RuntimeError("remote file observer returned a different path")
            return observation
        request = self._ssh_request(node_id, ("python3", "-c", _FILE_PROBE_SCRIPT, path, str(maximum_bytes)), 15)
        result = await self.ssh_runner.run(request)
        if result.return_code:
            raise RuntimeError("remote file observation failed")
        try:
            payload = json.loads(result.stdout.decode("utf-8"))
            content = payload.get("content")
            if content is not None:
                content = base64.b64decode(content, validate=True)
            return ManagedFileObservation(path=path, content=content, **{key: payload[key] for key in ("exists", "regular", "symlink", "owner_uid", "mode") if key in payload})
        except (ValueError, KeyError, TypeError, UnicodeError) as error:
            raise RuntimeError("remote file observation returned invalid data") from error

    async def podman_socket_ready(self, *, node_id: int) -> bool:
        return await self._successful(node_id, ("systemctl", "is-active", "--quiet", "podman.socket"), 15)

    async def quadlet_generator_ready(self, *, node_id: int) -> bool:
        return await self._successful(node_id, ("test", "-d", "/run/systemd/generator"), 15)

    async def generated_units(self, *, node_id: int, units: tuple[str, ...]) -> frozenset[str]:
        expected = {validate_managed_unit(unit) for unit in units}
        request = self._ssh_request(node_id, ("systemctl", "list-unit-files", "--no-legend", "--no-pager"), 15)
        result = await self.ssh_runner.run(request)
        if result.return_code:
            return frozenset()
        found = {line.split()[0] for line in result.stdout.decode("utf-8", errors="replace").splitlines() if line.split()}
        return frozenset(found & expected)

    async def unit_states(self, *, node_id: int, units: tuple[str, ...]) -> Mapping[str, bool]:
        states = {}
        for unit in units:
            validate_managed_unit(unit)
            states[unit] = await self._successful(node_id, ("systemctl", "is-active", "--quiet", unit), 15)
        return states

    async def endpoint_ready(self, *, node_id: int, endpoint_ref: str) -> bool:
        probe = self.endpoint_probes.get(endpoint_ref)
        if probe is None:
            raise ValueError("endpoint reference is not allowlisted")
        return bool(await probe(node=self._node(node_id), endpoint_ref=endpoint_ref))

    async def cleanup_executor(self, *, node_id: int, unit: str, paths: tuple[str, ...]) -> CleanupProof:
        if not self.flags.cleanup_enabled:
            raise RuntimeMutationDisabled("maintenance executor cleanup is disabled")
        operation_id = _operation_from_executor_unit(unit)
        normalized_paths = validate_cleanup_paths(paths, operation_id)
        request = self._ssh_request(
            node_id,
            ("python3", "-c", _CLEANUP_SCRIPT, unit, *normalized_paths),
            45,
        )
        result = await self.ssh_runner.run(request)
        return CleanupProof(status=CleanupStatus.VERIFIED if result.return_code == 0 else CleanupStatus.FAILED)

    async def _successful(self, node_id: int, argv: Sequence[str], timeout_seconds: int) -> bool:
        result = await self.ssh_runner.run(self._ssh_request(node_id, argv, timeout_seconds))
        return result.return_code == 0


def _operation_from_executor_unit(unit: str) -> str:
    match = re.fullmatch(r"ecp-maintenance-resume@([0-9a-f]{32})\.service", unit)
    if not match:
        raise ValueError("executor cleanup unit is outside the ownership boundary")
    operation_id = match.group(1)
    if executor_instance_unit(operation_id) != unit:
        raise ValueError("executor cleanup unit is invalid")
    return operation_id
