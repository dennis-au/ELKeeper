"""Cluster membership network policy, independent of FastAPI."""

from __future__ import annotations

import ipaddress


def valid_ipv4(value: str | None) -> bool:
    try:
        return ipaddress.ip_address(str(value).strip()).version == 4
    except (ValueError, TypeError):
        return False


def validate_membership_network(value) -> None:
    if not valid_ipv4(value.data_address) or not valid_ipv4(value.user_address):
        raise ValueError("Data and user addresses must be IPv4 addresses")
    if value.network_mode == "dedicated":
        if value.data_interface == value.user_interface:
            raise ValueError("Dedicated mode requires different data and user interfaces")
        if value.data_address == value.user_address:
            raise ValueError("Dedicated mode requires different data and user addresses")
    elif value.data_interface != value.user_interface or value.data_address != value.user_address:
        raise ValueError("Shared mode requires the same data and user interface and address")


def membership_ready(member) -> bool:
    try:
        data_interface = member["data_interface"]
        data_address = member["data_address"]
        user_interface = member["user_interface"]
        user_address = member["user_address"]
        network_mode = member["network_mode"]
    except (KeyError, IndexError, TypeError):
        return False
    return bool(
        data_interface
        and user_interface
        and valid_ipv4(data_address)
        and valid_ipv4(user_address)
        and network_mode in {"dedicated", "shared"}
        and ((network_mode == "dedicated" and data_interface != user_interface and data_address != user_address)
             or (network_mode == "shared" and data_interface == user_interface and data_address == user_address))
    )
