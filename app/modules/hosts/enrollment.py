"""Host enrollment naming and SSH host-key policy helpers."""

from __future__ import annotations

import re

from .repository import HostRepository


NODE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def enrollment_hostname(log: str) -> str:
    match = re.search(r"ECP_HOSTNAME=([A-Za-z0-9][A-Za-z0-9._-]{0,127})", log)
    return match.group(1) if match else ""


def unique_node_name(connection, requested: str, node_id: int) -> str:
    candidate = requested[:128]
    if not NODE_NAME_RE.fullmatch(candidate):
        return ""
    if not HostRepository.from_connection(connection).name_exists_in_connection(connection, candidate, node_id):
        return candidate
    suffix = f"-{node_id}"
    return candidate[:128 - len(suffix)] + suffix


def host_key_validation_enabled(node: dict) -> bool:
    """Honor explicit pins while retaining legacy trust until it is removed."""

    try:
        host_key = node["ssh_host_key"]
        auth_state = node["ssh_auth_state"]
        legacy_trust_disabled = bool(node["legacy_known_hosts_disabled"])
    except (IndexError, KeyError):
        host_key = node.get("ssh_host_key", "")
        auth_state = node.get("ssh_auth_state", "")
        legacy_trust_disabled = bool(node.get("legacy_known_hosts_disabled"))
    return bool(host_key) or (auth_state == "legacy" and not legacy_trust_disabled)


def ssh_host_key_args(node: dict, known_hosts: str) -> list[str]:
    if host_key_validation_enabled(node):
        return ["-o", f"UserKnownHostsFile={known_hosts}", "-o", "StrictHostKeyChecking=yes"]
    return ["-o", "UserKnownHostsFile=/dev/null", "-o", "StrictHostKeyChecking=no", "-o", "LogLevel=ERROR"]
