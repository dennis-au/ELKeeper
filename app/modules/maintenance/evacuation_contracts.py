"""Read-only evacuation and provider capability contracts.

Evacuation is deliberately planning-only.  The browser identifies the cluster
and the two hosts; the controller derives every safety predicate from durable
inventory and observations.  This prevents an optimistic client from turning
made-up capacity or provider values into an authorization decision.
"""

from __future__ import annotations

from enum import Enum

from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field

from app.modules.clusters import membership_ready, role_port_values


class ProviderCapability(str, Enum):
    NATIVE_PODMAN = "native_podman"
    ADOPTED_PODMAN = "adopted_podman"
    ECK_ENDPOINT = "eck_endpoint"
    EXTERNAL_API = "external_api"


class EvacuationPreview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    cluster_id: int | None = Field(default=None, ge=1)
    provider: ProviderCapability
    source_node_id: int = Field(ge=1)
    replacement_node_id: int | None = Field(default=None, ge=1)
    available_capacity: int | None = Field(default=None, ge=0)
    required_capacity: int = Field(ge=0)
    max_surge: int = Field(ge=0)
    mutation_allowed: bool
    blockers: tuple[str, ...] = ()
    evidence: dict[str, Any] = Field(default_factory=dict)


def build_evacuation_preview(
    *, provider: ProviderCapability | str, source_node_id: int,
    replacement_node_id: int | None, available_capacity: int,
    required_capacity: int, max_surge: int,
) -> EvacuationPreview:
    capability = ProviderCapability(provider)
    blockers: list[str] = []
    if replacement_node_id is not None and replacement_node_id == source_node_id:
        blockers.append("replacement_must_differ_from_source")
    if replacement_node_id is None:
        blockers.append("replacement_required")
    if available_capacity < required_capacity:
        blockers.append("capacity_insufficient")
    if capability in {ProviderCapability.ECK_ENDPOINT, ProviderCapability.EXTERNAL_API}:
        blockers.append("provider_read_only")
    if max_surge < 0:
        blockers.append("invalid_max_surge")
    return EvacuationPreview(
        provider=capability,
        source_node_id=source_node_id,
        replacement_node_id=replacement_node_id,
        available_capacity=available_capacity,
        required_capacity=required_capacity,
        max_surge=max_surge,
        mutation_allowed=not blockers and capability in {
            ProviderCapability.NATIVE_PODMAN,
            ProviderCapability.ADOPTED_PODMAN,
        },
        blockers=tuple(blockers),
    )


def _json_object(value: object, default: dict[str, Any] | None = None) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    return dict(default or {})


def _role_ports(cluster: Mapping[str, Any], role: str) -> tuple[int, ...]:
    """Return only the inbound ports for a role, tolerating legacy records."""

    profiles = _json_object(cluster.get("role_ports"))
    try:
        values = role_port_values(profiles, role)
    except (KeyError, TypeError, ValueError):
        return ()
    return tuple(value for value in values if isinstance(value, int) and 1 <= value <= 65535)


def _runtime_has_membership_addresses(runtime: Mapping[str, Any] | None, membership: Mapping[str, Any]) -> bool:
    if not runtime:
        return False
    interfaces = _json_object(runtime.get("network_interfaces"))
    for interface, address in (
        (membership.get("data_interface"), membership.get("data_address")),
        (membership.get("user_interface"), membership.get("user_address")),
    ):
        values = interfaces.get(interface)
        if not interface or not address or not isinstance(values, (list, tuple, set)) or address not in values:
            return False
    return bool(runtime.get("initialized")) and bool(runtime.get("reachable"))


def build_inventory_evacuation_preview(inventory: Mapping[str, Any]) -> EvacuationPreview:
    """Build a fail-closed placement preview from controller-owned evidence.

    ``inventory`` is supplied by the maintenance read projection.  It contains
    only data read from controller persistence; none of the capacity, provider,
    port, or policy evidence is accepted from the request body.
    """

    cluster = _json_object(inventory.get("cluster"))
    source = _json_object(inventory.get("source"))
    replacement = _json_object(inventory.get("replacement"))
    source_membership = _json_object(inventory.get("source_membership"))
    replacement_membership = _json_object(inventory.get("replacement_membership"))
    source_assignments = tuple(inventory.get("source_assignments") or ())
    replacement_assignments = tuple(inventory.get("replacement_assignments") or ())
    clusters = {
        int(item["id"]): _json_object(item)
        for item in tuple(inventory.get("clusters") or ())
        if isinstance(item, Mapping) and item.get("id") is not None
    }
    source_runtime = inventory.get("source_runtime") if isinstance(inventory.get("source_runtime"), Mapping) else None
    replacement_runtime = inventory.get("replacement_runtime") if isinstance(inventory.get("replacement_runtime"), Mapping) else None

    provider_value = cluster.get("provider_type", ProviderCapability.NATIVE_PODMAN.value)
    try:
        provider = ProviderCapability(str(provider_value))
    except ValueError:
        provider = ProviderCapability.EXTERNAL_API

    source_id = int(inventory.get("source_node_id") or 0)
    replacement_id_raw = inventory.get("replacement_node_id")
    replacement_id = int(replacement_id_raw) if replacement_id_raw is not None else None
    max_surge = int(inventory.get("max_surge") or 0)
    blockers: list[str] = []

    if not cluster:
        blockers.append("cluster_not_found")
    if not source:
        blockers.append("source_not_found")
    if not replacement:
        blockers.append("replacement_not_found")
    if replacement_id is None:
        blockers.append("replacement_required")
    elif replacement_id == source_id:
        blockers.append("replacement_must_differ_from_source")
    if source and not bool(source.get("enabled")):
        blockers.append("source_disabled")
    if replacement and not bool(replacement.get("enabled")):
        blockers.append("replacement_disabled")
    if not membership_ready(source_membership):
        blockers.append("source_network_incomplete")
    if not membership_ready(replacement_membership):
        blockers.append("replacement_network_incomplete")
    if source_membership and not _runtime_has_membership_addresses(source_runtime, source_membership):
        blockers.append("source_network_unverified")
    if replacement_membership and not _runtime_has_membership_addresses(replacement_runtime, replacement_membership):
        blockers.append("replacement_network_unverified")
    if not source_assignments:
        blockers.append("source_has_no_active_workloads")
    if provider in {ProviderCapability.ECK_ENDPOINT, ProviderCapability.EXTERNAL_API}:
        blockers.append("provider_read_only")

    zoning = _json_object(cluster.get("zoning"))
    if (
        zoning.get("mode") == "forced_awareness"
        and source.get("zone_id")
        and source.get("zone_id") == replacement.get("zone_id")
    ):
        blockers.append("replacement_zone_not_diverse")

    source_ports: set[int] = set()
    source_images_ready = True
    required_cpu = 0.0
    required_memory_bytes = 0
    managed_storage_count = 0
    resource_evidence_complete = bool(source_assignments)
    for assignment in source_assignments:
        if not isinstance(assignment, Mapping):
            resource_evidence_complete = False
            continue
        source_ports.update(_role_ports(cluster, str(assignment.get("role") or "")))
        resource = _json_object(assignment.get("resource"))
        try:
            required_cpu += float(resource.get("cpu"))
        except (TypeError, ValueError):
            resource_evidence_complete = False
        try:
            required_memory_bytes += int(resource.get("memory_bytes"))
        except (TypeError, ValueError):
            resource_evidence_complete = False
        if resource.get("storage_managed") is True:
            managed_storage_count += 1
        else:
            resource_evidence_complete = False
        observation = _json_object(assignment.get("observation"))
        if not observation.get("image") or not observation.get("digest") or not observation.get("cached"):
            source_images_ready = False

    replacement_ports: set[int] = set()
    for assignment in replacement_assignments:
        if not isinstance(assignment, Mapping):
            continue
        assignment_cluster = clusters.get(int(assignment.get("cluster_id") or 0), {})
        replacement_ports.update(_role_ports(assignment_cluster, str(assignment.get("role") or "")))
    if source_ports.intersection(replacement_ports):
        blockers.append("replacement_port_conflict")
    if not resource_evidence_complete:
        blockers.append("resource_evidence_incomplete")
    if not source_images_ready:
        blockers.append("image_evidence_incomplete")

    # Host capacity is not currently persisted as an allocatable controller
    # resource.  Advertising a numeric value here would make the future
    # executor trust a browser-era fiction, so keep this explicit and blocked.
    available_capacity: int | None = None
    blockers.append("replacement_capacity_unobserved")
    required_capacity = len(source_assignments)

    evidence = {
        "source_workloads": len(source_assignments),
        "replacement_workloads": len(replacement_assignments),
        "source_ports": sorted(source_ports),
        "replacement_ports": sorted(replacement_ports),
        "required_cpu_cores": round(required_cpu, 3),
        "required_memory_bytes": required_memory_bytes,
        "managed_storage_workloads": managed_storage_count,
        "capacity_observed": False,
        "source_network_verified": "source_network_unverified" not in blockers,
        "replacement_network_verified": "replacement_network_unverified" not in blockers,
        "image_evidence_complete": source_images_ready,
        "resource_evidence_complete": resource_evidence_complete,
    }
    return EvacuationPreview(
        cluster_id=int(cluster["id"]) if cluster.get("id") is not None else None,
        provider=provider,
        source_node_id=source_id,
        replacement_node_id=replacement_id,
        available_capacity=available_capacity,
        required_capacity=required_capacity,
        max_surge=max_surge,
        mutation_allowed=False,
        blockers=tuple(dict.fromkeys(blockers)),
        evidence=evidence,
    )


__all__ = [
    "EvacuationPreview",
    "ProviderCapability",
    "build_evacuation_preview",
    "build_inventory_evacuation_preview",
]
