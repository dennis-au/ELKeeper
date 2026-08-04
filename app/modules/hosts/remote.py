"""Host-owned remote inspection helpers.

The command transport is injected so the host module stays independent from
the controller's SSH pool while retaining the existing compatibility seams.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable


def ssh_error_summary(error: object) -> str:
    """Map untrusted SSH client output to a concise operator-safe diagnosis."""

    message = " ".join(str(error).replace("\r", "\n").split()).lower()
    if "permission denied" in message or "too many authentication failures" in message:
        return "Controller SSH key authentication failed"
    if "host key verification failed" in message or "remote host identification has changed" in message:
        return "SSH host key verification failed"
    if "connection timed out" in message or "operation timed out" in message:
        return "SSH connection timed out"
    if "connection refused" in message:
        return "SSH service refused the connection"
    if "no route to host" in message or "network is unreachable" in message:
        return "SSH host is unreachable"
    if "could not resolve hostname" in message:
        return "SSH address cannot be resolved"
    return "SSH connection failed"


class HostRemoteInspectionService:
    def __init__(
        self,
        *,
        active_key_path: Callable[[], str],
        known_hosts_path: Callable[[list[int] | None], str],
        host_key_args: Callable[[dict, str], list[str]],
        remote_command: Callable[..., Awaitable[bytes]],
        parse_counters: Callable[[bytes], dict],
    ):
        self._active_key_path = active_key_path
        self._known_hosts_path = known_hosts_path
        self._host_key_args = host_key_args
        self._remote_command = remote_command
        self._parse_counters = parse_counters

    def ssh_args(self, node: dict) -> list[str]:
        key_path = self._active_key_path()
        known_hosts = self._known_hosts_path([node["id"]] if node.get("id") else None)
        args = [
            "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=15",
            "-i", key_path, "-p", str(node["ssh_port"]),
        ]
        args += self._host_key_args(node, known_hosts)
        return args + [f"{node['ssh_user']}@{node['address']}"]

    async def identity(self, node: dict) -> tuple[str, str]:
        script = (
            "if test -r /etc/os-release; then . /etc/os-release; "
            "printf 'ECP_OS=%s\\n' \"${PRETTY_NAME:-${NAME:-unknown}}\"; "
            "else printf 'ECP_OS=%s\\n' \"$(uname -sr)\"; fi; "
            "if command -v podman >/dev/null 2>&1; then podman --version | sed 's/^/ECP_PODMAN=/'; "
            "else printf 'ECP_PODMAN=\\n'; fi"
        )
        output = (await self._remote_command(node, script)).decode(errors="replace")
        values = {}
        for line in output.splitlines():
            if line.startswith("ECP_OS="):
                values["os_name"] = line[7:].strip()[:256]
            elif line.startswith("ECP_PODMAN="):
                values["podman_version"] = line[11:].replace("podman version ", "", 1).strip()[:128]
        return values.get("os_name", "unknown"), values.get("podman_version", "")

    async def resource_counters(self, node: dict) -> dict:
        script = r'''
awk '/^cpu / { total=0; for (i=2; i<=NF; i++) total+=$i; print "cpu_total=" total; print "cpu_idle=" $5+$6; exit }' /proc/stat
awk '/^MemTotal:/ { total=$2*1024 } /^MemAvailable:/ { available=$2*1024 } END { print "memory_total_bytes=" total; print "memory_available_bytes=" available }' /proc/meminfo
awk 'NR>2 { iface=$1; sub(/:$/, "", iface); if (iface != "lo") { rx+=$2; tx+=$10 } } END { print "network_rx_bytes=" rx; print "network_tx_bytes=" tx }' /proc/net/dev
for path in /sys/block/*; do
  test -e "$path" || continue
  device=${path##*/}
  case "$device" in loop*|ram*|zram*|fd*|sr*|md*|dm-*|nbd*) continue ;; esac
  test -e "$path/device" || continue
  awk -v device="$device" '$3 == device { read += $6 * 512; write += $10 * 512 } END { print read, write }' /proc/diskstats
done | awk '{ read += $1; write += $2 } END { print "disk_read_bytes=" read; print "disk_write_bytes=" write }'
'''
        return self._parse_counters(await self._remote_command(node, script))


__all__ = ["HostRemoteInspectionService", "ssh_error_summary"]
