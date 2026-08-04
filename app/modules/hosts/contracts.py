"""DTOs and pure validation for managed host inventory."""

from __future__ import annotations

import ipaddress
import re

from pydantic import BaseModel, Field, field_validator, model_validator


NODE_NAME_RE = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
ZONE_ID_RE = r"^[a-z0-9][a-z0-9._-]{0,63}$"


class HostAddress(BaseModel):
    address: str

    @field_validator("address")
    @classmethod
    def must_be_ip(cls, value: str) -> str:
        try:
            return str(ipaddress.ip_address(value.strip()))
        except ValueError as error:
            raise ValueError("SSH address must be an IPv4 or IPv6 address") from error


class HostSpec(HostAddress):
    name: str = Field(pattern=NODE_NAME_RE)
    ssh_port: int = Field(default=22, ge=1, le=65535)
    ssh_user: str = Field(default="root", pattern=r"^[A-Za-z_][A-Za-z0-9_-]{0,31}$")
    enabled: bool = True
    zone_id: str | None = None


class Node(HostSpec):
    address: str = Field(min_length=1, max_length=255)

    @field_validator("zone_id", mode="before")
    @classmethod
    def valid_zone_id(cls, value):
        if value is None or not str(value).strip():
            return None
        zone_id = str(value).strip().lower()
        if not re.fullmatch(ZONE_ID_RE, zone_id):
            raise ValueError("Zone IDs may contain lowercase letters, numbers, dots, underscores, and hyphens")
        return zone_id


class NodeEnrollment(Node):
    name: str = Field(default="", max_length=128)
    ssh_host_key: str = Field(default="", max_length=8192)
    auth_method: str = Field(default="controller_key", pattern=r"^(controller_key|password)$")
    password: str | None = Field(default=None, max_length=1024)
    install_controller_key: bool = True
    zone_cluster_id: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_credentials(self):
        if self.name and not re.fullmatch(NODE_NAME_RE, self.name):
            raise ValueError("Inventory name may contain letters, numbers, dots, underscores, and hyphens")
        if self.auth_method == "password" and not self.password:
            raise ValueError("A password is required for password bootstrap")
        if self.auth_method == "controller_key" and self.password:
            raise ValueError("Passwords may only be supplied for password bootstrap")
        return self


class NodePasswordTest(HostAddress):
    address: str = Field(min_length=1, max_length=255)
    ssh_port: int = Field(default=22, ge=1, le=65535)
    ssh_user: str = Field(default="root", pattern=r"^[A-Za-z_][A-Za-z0-9_-]{0,31}$")
    ssh_host_key: str = Field(default="", max_length=8192)
    password: str = Field(min_length=1, max_length=1024)


class NodeUpdate(Node):
    ssh_host_key: str | None = Field(default=None, max_length=8192)
