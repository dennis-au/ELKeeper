import asyncio
import base64
import os
import ssl
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.modules.clusters import ClusterRepository
from app.modules.certificates import (
    ca_ssl_context as build_ca_ssl_context,
    cluster_ca_path,
    invalidate_cluster_ca as remove_cluster_ca,
)
from app.modules.hosts import HostRemoteInspectionService, HostRepository, enabled_host, host_network_interfaces as collect_host_network_interfaces
from app.modules.hosts import ssh_error_summary as summarize_ssh_error
from app.modules.hosts import storage_mount_entries as hosts_storage_mount_entries
from app.modules.hosts import storage_mount_eligibility as hosts_storage_mount_eligibility
from app.modules.hosts import storage_mounts as hosts_storage_mounts
from app.modules.hosts import parse_network_interfaces as parse_host_network_interfaces
from app.modules.observability import (
    HOST_RESOURCE_COUNTERS,
    NODE_TYPE_ORDER,
    ObservabilityRepository,
    TelemetrySupervisor,
    PodmanTunnel as ObservabilityPodmanTunnel,
    SSHConnectionPool as ObservabilitySSHConnectionPool,
    container_name as observability_container_name,
    container_stats as observability_container_stats,
    elastic_node_type as observability_elastic_node_type,
    host_resource_rates as observability_host_resource_rates,
    node_breakdown as observability_node_breakdown,
    parse_host_resource_counters as observability_parse_host_resource_counters,
    register_telemetry,
    TelemetryDependencies,
    TelemetryManager as ObservabilityTelemetryManager,
    zone_breakdown as observability_zone_breakdown,
)
from app.modules.orchestration import ansible_playbook
from app.modules.platform.audit import write_event_in_connection
from app.modules.platform.auth import signed_scope_token as platform_signed_scope_token
from app.modules.platform.auth import valid_scope_token as platform_valid_scope_token
from app.modules.platform.runs import completed_run as platform_completed_run
from app.modules.secrets import RemoteSecretMetadataService
from app.modules.versions import VersionRepository
from app.modules.workloads import WorkloadRepository


router = APIRouter()
RUNTIME = Path(os.getenv("APP_RUNTIME_DIR", "/run/elastic-control"))
CA_CACHE = Path(os.getenv("APP_DATA_DIR", "/var/lib/elastic-control")) / "cluster-cas"
COLLECT_INTERVAL = int(os.getenv("TELEMETRY_INTERVAL_SECONDS", "5"))
FAST_COLLECT_INTERVAL = max(1, int(os.getenv("TELEMETRY_FAST_INTERVAL_SECONDS", str(COLLECT_INTERVAL))))
SLOW_COLLECT_INTERVAL = max(FAST_COLLECT_INTERVAL, int(os.getenv("TELEMETRY_SLOW_INTERVAL_SECONDS", "30")))
CLUSTER_COLLECT_INTERVAL = max(FAST_COLLECT_INTERVAL, int(os.getenv("CLUSTER_TELEMETRY_INTERVAL_SECONDS", "15")))
MAX_FAST_HOST_PROBES = max(1, int(os.getenv("TELEMETRY_FAST_MAX_CONCURRENCY", "5")))
MAX_SLOW_HOST_PROBES = max(1, int(os.getenv("TELEMETRY_SLOW_MAX_CONCURRENCY", "3")))
POLL_JITTER_SECONDS = max(0.0, float(os.getenv("TELEMETRY_POLL_JITTER_SECONDS", "0.25")))
SSH_CONTROL_PERSIST_SECONDS = max(30, int(os.getenv("SSH_CONTROL_PERSIST_SECONDS", "120")))
HOST_RESOURCE_HISTORY_SECONDS = 900
STREAM_TOKEN_TTL = 600
REVEAL_GRANT_TTL = 60


@dataclass(frozen=True)
class ConsoleRuntimeDependencies:
    data: Path
    db_factory: Callable
    secret_key: str
    active_key_path: Callable
    known_hosts_path: Callable
    host_key_args: Callable
    valid_storage_path: Callable
    workload_name: Callable
    image_version: Callable
    open_config: Callable
    seal_config: Callable
    cluster_record: Callable
    cluster_settings_service: Callable[[], Any]
    secrets_catalog_service: Callable[[], Any]


_dependencies: ConsoleRuntimeDependencies | None = None


def _deps() -> ConsoleRuntimeDependencies:
    if _dependencies is None:
        raise RuntimeError("Console runtime dependencies have not been configured")
    return _dependencies


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def completed_run(kind, target, message):
    return platform_completed_run(_deps().db_factory, kind, target, f"[{utc_now()}] {message}")


def audit(username, action, cluster_id=None, item_id="", detail=""):
    with _deps().db_factory() as con:
        write_event_in_connection(
            con,
            username,
            action,
            cluster_id=cluster_id,
            item_id=item_id,
            detail=detail,
        )


def signed_scope_token(scope):
    return platform_signed_scope_token(scope, key=_deps().secret_key)


def valid_scope_token(token, scope, ttl=STREAM_TOKEN_TTL):
    return platform_valid_scope_token(token, scope, key=_deps().secret_key, ttl=ttl)


def ca_ssl_context(ca_path):
    """Compatibility wrapper preserving the console SSL patch seam."""

    return build_ca_ssl_context(ca_path, ssl_module=ssl)


def invalidate_cluster_ca(cluster_id):
    """Compatibility wrapper for certificate-owned CA cache cleanup."""

    remove_cluster_ca(CA_CACHE, cluster_id)


def ssh_args(node):
    return host_remote_service().ssh_args(node)


class SSHConnectionPool(ObservabilitySSHConnectionPool):
    """Compatibility facade for the observability-owned SSH pool."""

    def __init__(self):
        super().__init__(runtime_dir=RUNTIME, ssh_args=ssh_args, persist_seconds=SSH_CONTROL_PERSIST_SECONDS)


ssh_pool = SSHConnectionPool()


async def remote_command(node, *command, timeout=8):
    return await ssh_pool.run(node, command, timeout=timeout)


def host_remote_service():
    return HostRemoteInspectionService(
        active_key_path=_deps().active_key_path,
        known_hosts_path=lambda node_ids: _deps().known_hosts_path(node_ids),
        host_key_args=_deps().host_key_args,
        remote_command=remote_command,
        parse_counters=observability_parse_host_resource_counters,
    )


def parse_network_interfaces(payload):
    """Compatibility alias for the host-owned network parser."""

    return parse_host_network_interfaces(payload)


async def host_network_interfaces(node):
    """Compatibility wrapper retaining the console runtime seam."""

    return await collect_host_network_interfaces(node, remote_command)


def ssh_error_summary(error):
    """Compatibility wrapper for host-owned SSH error classification."""

    return summarize_ssh_error(error)


async def host_identity(node):
    return await host_remote_service().identity(node)


async def host_resource_counters(node):
    return await host_remote_service().resource_counters(node)


def storage_mount_entries(filesystems):
    yield from hosts_storage_mount_entries(filesystems)


def storage_mount_eligibility(target, fstype, options, available_bytes):
    return hosts_storage_mount_eligibility(
        target,
        fstype,
        options,
        available_bytes,
        valid_storage_path=_deps().valid_storage_path,
    )


def storage_mounts(payload):
    return hosts_storage_mounts(payload, valid_storage_path=_deps().valid_storage_path)


def cluster_awaits_data_role(cluster):
    assignments = cluster["assignments"]
    return any(item["role"] == "master" for item in assignments) and not any(item["role"] in {"hot", "warm"} for item in assignments)


class PodmanTunnel(ObservabilityPodmanTunnel):
    """Compatibility facade for the observability-owned Podman tunnel."""

    def __init__(self, node_id):
        super().__init__(node_id, runtime_dir=RUNTIME, ssh_args=ssh_args)


def container_name(item):
    """Compatibility wrapper for observability-owned container parsing."""
    return observability_container_name(item)


def container_stats(item):
    """Compatibility wrapper for observability-owned container metrics."""
    return observability_container_stats(item)


def parse_host_resource_counters(output):
    """Compatibility wrapper for observability-owned host counters."""
    return observability_parse_host_resource_counters(output)


def host_resource_rates(previous, current):
    """Compatibility wrapper for observability-owned rate calculation."""
    return observability_host_resource_rates(previous, current)


def elastic_node_type(roles):
    return observability_elastic_node_type(roles)


def node_breakdown(node_stats, allocation):
    return observability_node_breakdown(node_stats, allocation)


def zone_breakdown(nodes):
    return observability_zone_breakdown(nodes)


def _telemetry_dependencies():
    """Build an injected collector contract from current compatibility hooks."""

    return TelemetryDependencies(
        db_factory=_deps().db_factory,
        workload_name=_deps().workload_name,
        image_version=_deps().image_version,
        open_config=_deps().open_config,
        seal_config=_deps().seal_config,
        cluster_record=_deps().cluster_record,
        runtime=RUNTIME,
        ca_cache=CA_CACHE,
        fast_collect_interval=FAST_COLLECT_INTERVAL,
        slow_collect_interval=SLOW_COLLECT_INTERVAL,
        cluster_collect_interval=CLUSTER_COLLECT_INTERVAL,
        max_fast_host_probes=MAX_FAST_HOST_PROBES,
        max_slow_host_probes=MAX_SLOW_HOST_PROBES,
        poll_jitter_seconds=POLL_JITTER_SECONDS,
        host_resource_history_seconds=HOST_RESOURCE_HISTORY_SECONDS,
        cluster_repository=ClusterRepository,
        host_repository=HostRepository,
        observability_repository=ObservabilityRepository,
        version_repository=VersionRepository,
        workload_repository=WorkloadRepository,
        podman_tunnel_cls=PodmanTunnel,
        ssh_pool=ssh_pool,
        remote_command=lambda *args, **kwargs: remote_command(*args, **kwargs),
        host_identity=lambda *args, **kwargs: host_identity(*args, **kwargs),
        host_network_interfaces=lambda *args, **kwargs: host_network_interfaces(*args, **kwargs),
        host_resource_counters=lambda *args, **kwargs: host_resource_counters(*args, **kwargs),
        ssh_error_summary=ssh_error_summary,
        container_name=container_name,
        container_stats=container_stats,
        host_resource_rates=lambda *args, **kwargs: host_resource_rates(*args, **kwargs),
        node_breakdown=node_breakdown,
        zone_breakdown=zone_breakdown,
        ca_ssl_context=lambda *args, **kwargs: ca_ssl_context(*args, **kwargs),
        cluster_ca_path=cluster_ca_path,
        invalidate_cluster_ca=invalidate_cluster_ca,
        utc_now=utc_now,
        cluster_awaits_data_role=cluster_awaits_data_role,
    )


class TelemetryManager(ObservabilityTelemetryManager):
    """Compatibility constructor for callers that historically used no args."""

    def __init__(self):
        super().__init__(_telemetry_dependencies())


ssh_pool = None
telemetry = None


def configure_runtime(dependencies: ConsoleRuntimeDependencies) -> None:
    """Configure the legacy console facade from explicit application contracts."""

    global _dependencies, RUNTIME, CA_CACHE, ssh_pool, telemetry
    _dependencies = dependencies
    RUNTIME = dependencies.data / "runtime"
    CA_CACHE = dependencies.data / "cluster-cas"
    ssh_pool = SSHConnectionPool()
    telemetry = TelemetrySupervisor(TelemetryManager())
    register_telemetry(telemetry)


def enabled_node(node_id):
    node = enabled_host(_deps().db_factory, node_id)
    if not node:
        raise HTTPException(404, "Enabled host not found")
    return node


def cluster_settings_impl(cluster_id: int):
    """Compatibility delegate for the cluster-owned settings service."""

    return _deps().cluster_settings_service().get(cluster_id)


async def update_cluster_settings_impl(cluster_id: int, settings: Any, username: str):
    """Compatibility delegate for the cluster-owned settings service."""

    return await _deps().cluster_settings_service().update(cluster_id, settings, username)


def sensitive_catalog(_connection, cluster_id):
    """Compatibility delegate for the secrets-owned catalog service."""

    return _deps().secrets_catalog_service().catalog(cluster_id)


async def remote_sensitive_metadata(item):
    return await RemoteSecretMetadataService(remote_command).inspect(item)
