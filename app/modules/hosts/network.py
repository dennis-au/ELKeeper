"""Host network inventory helpers.

The host module owns the shape of ``ip -j address show`` data.  Transport
details remain injected so this parser can be exercised without an SSH
connection and without coupling the host domain to the controller runtime.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable


def parse_network_interfaces(payload: object) -> dict[str, list[str]]:
    """Normalize structured ``ip -j address`` output by interface name.

    Invalid top-level payloads are rejected, while malformed individual
    interface/address entries are ignored.  Addresses are de-duplicated and
    sorted to keep persisted observations deterministic.
    """

    if not isinstance(payload, list):
        raise ValueError("ip address output must be a JSON array")
    interfaces: dict[str, list[str]] = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        name = item.get("ifname")
        if not isinstance(name, str) or not name:
            continue
        addresses: list[str] = []
        for address in item.get("addr_info") or []:
            if not isinstance(address, dict):
                continue
            local = address.get("local")
            if isinstance(local, str) and local:
                addresses.append(local)
        interfaces[name] = sorted(set(addresses))
    return interfaces


async def host_network_interfaces(
    node: dict,
    remote_command: Callable[..., Awaitable[bytes]],
) -> dict[str, list[str]]:
    """Collect and parse network interfaces through an injected SSH command."""

    output = await remote_command(node, "ip", "-j", "address", "show")
    try:
        payload = json.loads(output.decode(errors="strict"))
        return parse_network_interfaces(payload)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise RuntimeError("Host network inventory returned invalid JSON") from error
