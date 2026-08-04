"""Provider ownership and capability contracts for maintenance."""

from __future__ import annotations

import json
from enum import Enum
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.modules.maintenance.models import MaintenanceBackend, ProviderType


class OwnershipState(str, Enum):
    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    READ_ONLY = "read_only"


class ProviderCapability(str, Enum):
    HOST_MUTATION = "host_mutation"
    WORKLOAD_MUTATION = "workload_mutation"
    CLUSTER_SETTINGS = "cluster_settings"
    LIFECYCLE_API = "lifecycle_api"
    OBSERVATION = "observation"
    RECOVERY = "recovery"


class ProviderCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    host_mutation: bool = False
    workload_mutation: bool = False
    cluster_settings: bool = False
    lifecycle_api: bool = False
    observation: bool = True
    recovery: bool = False


_PROVIDER_CAPABILITY_MATRIX = {
    ProviderType.NATIVE_PODMAN: ProviderCapabilities(host_mutation=True, workload_mutation=True, cluster_settings=True, lifecycle_api=True, observation=True, recovery=True),
    ProviderType.ADOPTED_PODMAN: ProviderCapabilities(workload_mutation=True, cluster_settings=True, lifecycle_api=True, observation=True, recovery=True),
    ProviderType.EXTERNAL_API: ProviderCapabilities(cluster_settings=True, lifecycle_api=True, observation=True, recovery=True),
    ProviderType.ECK_ENDPOINT: ProviderCapabilities(observation=True),
}


def capability_matrix(provider_type: ProviderType | str) -> dict[str, bool]:
    return _PROVIDER_CAPABILITY_MATRIX[ProviderType(provider_type)].model_dump()


class ProviderProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_type: ProviderType = ProviderType.NATIVE_PODMAN
    ownership_state: OwnershipState = OwnershipState.VERIFIED
    maintenance_backend: MaintenanceBackend = MaintenanceBackend.DOCUMENTED_ROLLING
    capability_overrides: Mapping[str, bool] = Field(default_factory=dict)
    connection_references: Mapping[str, Any] = Field(default_factory=dict)
    revision: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def validate_backend(self):
        if self.provider_type is ProviderType.ECK_ENDPOINT and self.maintenance_backend is not MaintenanceBackend.NONE:
            raise ValueError("ECK endpoint-only providers must use the none maintenance backend")
        return self

    @property
    def capabilities(self) -> ProviderCapabilities:
        maximum = capability_matrix(self.provider_type)
        requested = {str(key): bool(value) for key, value in self.capability_overrides.items()}
        unknown = sorted(set(requested) - set(maximum))
        if unknown:
            raise ValueError("Unknown provider capabilities: " + ", ".join(unknown))
        effective = {name: maximum[name] and requested.get(name, maximum[name]) for name in maximum}
        if self.ownership_state is not OwnershipState.VERIFIED:
            effective = {name: value if name == ProviderCapability.OBSERVATION.value else False for name, value in effective.items()}
        return ProviderCapabilities.model_validate(effective)


def require_capability(profile: ProviderProfile, capability: ProviderCapability | str) -> None:
    selected = ProviderCapability(capability)
    if not getattr(profile.capabilities, selected.value):
        raise PermissionError(f"Provider {profile.provider_type.value} in {profile.ownership_state.value} state does not allow {selected.value}")


def provider_profile_from_record(record: Mapping[str, Any]) -> ProviderProfile:
    record = dict(record)

    def decoded(name: str) -> Mapping[str, Any]:
        value = record.get(name, {})
        if isinstance(value, Mapping):
            return value
        try:
            loaded = json.loads(value or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            loaded = {}
        return loaded if isinstance(loaded, Mapping) else {}

    return ProviderProfile(
        provider_type=record.get("provider_type") or ProviderType.NATIVE_PODMAN,
        ownership_state=record.get("ownership_state") or OwnershipState.VERIFIED,
        maintenance_backend=record.get("maintenance_backend") or MaintenanceBackend.DOCUMENTED_ROLLING,
        capability_overrides=decoded("provider_capabilities_json"),
        connection_references=decoded("provider_connection_json"),
        revision=record.get("provider_revision") or 1,
    )
