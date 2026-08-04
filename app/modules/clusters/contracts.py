"""Cluster settings, topology, and membership request DTOs."""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator, model_validator

from app.modules.maintenance.contracts import MaintenanceBackend, OwnershipState, ProviderProfile, ProviderType

from .ports import PortProfile, RolePortProfile, default_role_ports


ZONE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


class NetworkDefaults(BaseModel):
    mode: str = Field(default="shared", pattern=r"^(dedicated|shared)$")


class ClusterProviderUpdate(BaseModel):
    """Validated provider ownership settings for an existing cluster."""

    model_config = {"extra": "forbid"}

    expected_revision: int = Field(ge=1)
    provider_type: ProviderType
    ownership_state: OwnershipState
    maintenance_backend: MaintenanceBackend
    capability_overrides: dict[str, bool] = Field(default_factory=dict)
    connection_references: dict[str, str] = Field(default_factory=dict)
    expected_cluster_uuid: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{8,128}$")

    @field_validator("connection_references")
    @classmethod
    def validate_connection_references(cls, value):
        allowed = {"endpoint_ref", "ca_ref", "credential_ref", "provider_resource_id"}
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise ValueError("Unsupported provider connection references: " + ", ".join(unknown))
        normalized = {}
        for key, reference in value.items():
            reference = str(reference).strip()
            if not reference or len(reference) > 256 or any(char in reference for char in "\r\n\0"):
                raise ValueError(f"Provider connection reference {key} is invalid")
            normalized[key] = reference
        return normalized

    @model_validator(mode="after")
    def validate_provider_profile(self):
        ProviderProfile(
            provider_type=self.provider_type,
            ownership_state=self.ownership_state,
            maintenance_backend=self.maintenance_backend,
            capability_overrides=self.capability_overrides,
            connection_references=self.connection_references,
            revision=self.expected_revision,
        ).capabilities
        return self


class ZoningConfig(BaseModel):
    mode: str = Field(default="disabled", pattern=r"^(disabled|awareness|forced_awareness)$")
    zones: list[str] = Field(default_factory=list, max_length=16)

    @field_validator("zones", mode="before")
    @classmethod
    def normalize_zones(cls, value):
        return [] if value is None else [str(zone).strip().lower() for zone in value]

    @model_validator(mode="after")
    def valid_zones(self):
        if any(not ZONE_ID_RE.fullmatch(zone) for zone in self.zones):
            raise ValueError("Zone IDs may contain lowercase letters, numbers, dots, underscores, and hyphens")
        if len(self.zones) != len(set(self.zones)):
            raise ValueError("Zone IDs must be unique")
        if self.mode != "disabled" and len(self.zones) < 2:
            raise ValueError("Zone awareness requires at least two zones")
        return self


class HostZoneInput(BaseModel):
    cluster_id: int = Field(ge=1)
    zone_id: str

    @field_validator("zone_id", mode="before")
    @classmethod
    def valid_zone_id(cls, value):
        zone_id = str(value).strip().lower()
        if not ZONE_ID_RE.fullmatch(zone_id):
            raise ValueError("Choose a valid cluster-defined zone")
        return zone_id


class ElasticsearchSettings(BaseModel):
    allocation_enable: str = Field(default="all", pattern=r"^(all|primaries|new_primaries|none)$")
    rebalance_enable: str = Field(default="all", pattern=r"^(all|primaries|replicas|none)$")
    disk_watermark_low: str = Field(default="85%", pattern=r"^[1-9][0-9]?%$")
    disk_watermark_high: str = Field(default="90%", pattern=r"^[1-9][0-9]?%$")
    disk_watermark_flood_stage: str = Field(default="95%", pattern=r"^[1-9][0-9]?%$")
    recovery_max_bytes_per_sec: str = Field(default="40mb", pattern=r"^[1-9][0-9]*(?:kb|mb|gb)$")

    @model_validator(mode="after")
    def ordered_watermarks(self):
        values = [int(self.disk_watermark_low[:-1]), int(self.disk_watermark_high[:-1]), int(self.disk_watermark_flood_stage[:-1])]
        if values != sorted(values) or len(set(values)) != 3:
            raise ValueError("Disk watermarks must increase from low to high to flood stage")
        return self


class LogMonitoringInput(BaseModel):
    filebeat_enabled: bool


class ClusterInput(BaseModel):
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    ports: PortProfile = Field(default_factory=PortProfile)
    role_ports: RolePortProfile = Field(default_factory=RolePortProfile)
    theme_color: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")
    desired_version: str = Field(default="8.19.0", pattern=r"^\d+\.\d+\.\d+$")
    network_defaults: NetworkDefaults = Field(default_factory=NetworkDefaults)
    elasticsearch_settings: ElasticsearchSettings = Field(default_factory=ElasticsearchSettings)
    zoning: ZoningConfig = Field(default_factory=ZoningConfig)

    @model_validator(mode="before")
    @classmethod
    def derive_role_ports_from_legacy_ports(cls, value):
        if isinstance(value, dict) and "role_ports" not in value:
            result = dict(value)
            result["role_ports"] = default_role_ports(value.get("ports") or {})
            return result
        return value


class MembershipInput(BaseModel):
    node_id: int = Field(ge=1)
    network_mode: str = Field(default="dedicated", pattern=r"^(dedicated|shared)$")
    data_interface: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,64}$")
    data_address: str = Field(min_length=1, max_length=255)
    user_interface: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,64}$")
    user_address: str = Field(min_length=1, max_length=255)

    @model_validator(mode="before")
    @classmethod
    def reject_legacy_address(cls, value):
        if isinstance(value, dict) and "advertised_address" in value:
            raise ValueError("advertised_address has been replaced by distinct data_interface, data_address, user_interface, and user_address fields")
        return value
