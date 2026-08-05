import asyncio
import base64
import binascii
import calendar
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
from html.parser import HTMLParser
import ipaddress
import json
import os
import re
import secrets
import shlex
import sqlite3
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import Response
from pydantic import Field

from app.modules.maintenance.store import MaintenanceRepository as MaintenanceStore, install_maintenance_schema
from app.modules.maintenance.repository import MaintenanceRepository as MaintenanceReadRepository
from app.modules.maintenance.models import MaintenanceBackend, ProviderType
from app.modules.maintenance import (
    MaintenanceUpgradePlanningService,
    Phase2RebootAdapterFactory,
    workload_maintenance_progress_in_connection,
)
from app.modules.maintenance.provider import (
    OwnershipState,
    ProviderCapability,
    ProviderProfile,
    provider_profile_from_record,
    require_capability,
)
from app.modules.platform.audit import write_event
from app.modules.platform.audit import write_event_in_connection
from app.modules.platform.maintenance import MAINTENANCE_CAPABILITIES
from app.modules.platform.config import app_data_dir as platform_app_data_dir
from app.modules.platform.config import environment_flag as platform_environment_flag
from app.modules.platform.config import get_setting as platform_get_setting
from app.modules.platform.config import RuntimePaths
from app.modules.platform.db import connect as platform_db_connect
from app.modules.platform.runs import append_log as platform_append_log
from app.modules.platform.runs import any_active_ids_in_connection as platform_any_active_ids_in_connection
from app.modules.platform.runs import append_log_in_connection as platform_append_log_in_connection
from app.modules.platform.runs import completed_run as platform_completed_run
from app.modules.platform.runs import context_and_log_in_connection as platform_context_and_log_in_connection
from app.modules.platform.runs import create_run_in_connection as platform_create_run_in_connection
from app.modules.platform.runs import RunDescriptor as PlatformRunDescriptor
from app.modules.platform.runs import start_run_in_connection as platform_start_run_in_connection
from app.modules.platform.runs import update_run_status_in_connection as platform_update_run_status_in_connection
from app.modules.platform.runs import finish_run_in_connection as platform_finish_run_in_connection
from app.modules.platform.runs import mark_recovery_required_in_connection as platform_mark_recovery_required_in_connection
from app.modules.platform.runs import has_active_target_in_connection as platform_has_active_target_in_connection
from app.modules.platform.runs import rename_target_in_connection as platform_rename_target_in_connection
from app.modules.platform.runs import set_running_command_in_connection as platform_set_running_command_in_connection
from app.modules.platform.runs import status_in_connection as platform_status_in_connection
from app.modules.platform.runs import recovery_required_ids_in_connection as platform_recovery_required_ids_in_connection
from app.modules.platform.runs import recent_runs as platform_recent_runs
from app.modules.platform.runs import stream_run_events as platform_stream_run_events
from app.modules.platform.app import build_lifespan, install_security_headers, mount_static_assets
from app.modules.platform.command_runs import RunLifecycleService
from app.modules.platform.command_runs import execute_logged_command as platform_execute_logged_command
from app.modules.platform.command_runs import run_commands as platform_run_commands
from app.modules.platform.integration import PlatformRunOperations
from app.modules.platform.http import LoginPayload, build_router
from app.modules.maintenance.http import build_router as build_maintenance_router
from app.modules.maintenance.api import router as maintenance_router
from app.modules.platform.bootstrap import ControllerBootstrapService, apply_controller_schema_upgrades, bootstrap_controller_schema, complete_controller_bootstrap
from app.modules.platform.security import StoredSecretError
from app.modules.platform.security import digest as platform_digest
from app.modules.platform.security import open_config as platform_open_config
from app.modules.platform.security import open_secret as platform_open_secret
from app.modules.platform.security import redact_config as platform_redact_config
from app.modules.platform.security import seal_config as platform_seal_config
from app.modules.platform.security import seal_secret as platform_seal_secret
from app.modules.platform.security import valid_password as platform_valid_password
from app.modules.platform.auth import password_matches as platform_password_matches
from app.modules.platform.auth import read_token_piece as platform_read_token_piece
from app.modules.platform.auth import require_user as platform_require_user
from app.modules.platform.auth import run_events_token as platform_run_events_token
from app.modules.platform.auth import signed_token as platform_signed_token
from app.modules.platform.auth import token_piece as platform_token_piece
from app.modules.platform.auth import token_user as platform_token_user
from app.modules.platform.auth import valid_run_events_token as platform_valid_run_events_token
from app.modules.orchestration import ansible_module as orchestration_module
from app.modules.orchestration import ansible_playbook as orchestration_playbook
from app.modules.orchestration import redacted_command as orchestration_redacted_command
from app.modules.orchestration import stream_command as orchestration_stream_command
from app.modules.hosts import HostLifecycleOperations, HostOperations, HostRepository, Node, NodeEnrollment, NodePasswordTest, NodeUpdate, enrollment_hostname, host_key_validation_enabled, ssh_host_key_args, unique_node_name
from app.modules.hosts.orchestration import HostEnrollmentOrchestrator
from app.modules.controller_identity import ControllerIdentityOperations, ControllerIdentityRepository, ControllerKeyImportInput, ControllerPasswordInput, ControllerSettingsInput, KeyInstall, key_algorithm, normalize_ssh_host_key, parse_imported_private_key, public_key_fingerprint as identity_fingerprint, serialize_private_key
from app.modules.controller_identity.http import build_key_router, build_router as build_identity_router
from app.modules.workloads import WorkloadOperations, render_topology as workload_render_topology
from app.modules.workloads import AssignmentInput, ResourceInput, Targets, WorkloadChange, WorkloadChangeSet, WorkloadChangeValidator, WorkloadPayloadService, WorkloadPolicyService, WorkloadProjectionService, WorkloadRepository, WorkloadService
from app.modules.clusters import membership_ready as cluster_membership_ready
from app.modules.clusters import validate_membership_network as cluster_validate_membership_network
from app.modules.clusters import valid_ipv4 as cluster_valid_ipv4
from app.modules.clusters import ClusterInput, ClusterLifecycleService, ClusterPolicyService, ClusterProjectionService, ClusterProviderUpdate, ClusterRepository, ClusterSettingsService, ElasticsearchSettings, HostZoneInput, LogMonitoringInput, MembershipInput, MembershipOperations, NetworkDefaults, ZoningConfig, build_lifecycle_router as build_cluster_lifecycle_router
from app.modules.clusters import ZoningOperations
from app.modules.clusters.ports import PortProfile, RolePortProfile, default_role_ports, next_available_port, role_port_values, stored_role_ports, valid_port
from app.modules.versions import image_for_role as versions_image_for_role
from app.modules.versions import image_version as versions_image_version
from app.modules.versions import observation_is_fresh as versions_observation_is_fresh
from app.modules.versions import version_key as versions_version_key
from app.modules.versions import FilebeatReconcileWorker, VersionOperations, VersionRepository
from app.modules.versions.registry import ElasticRegistry
from app.modules.versions.registry import available_role_versions as registry_available_role_versions
from app.modules.versions.registry import available_versions as registry_available_versions
from app.modules.versions.registry import recommended_version as registry_recommended_version
from app.modules.versions.registry import repositories as registry_repositories
from app.modules.versions.registry import version_cursor as registry_version_cursor_value
from app.modules.observability.http import build_router as build_observability_router
from app.modules.observability import runtime_observation
from app.modules.certificates import (
    CertificateInventoryService,
    CertificateLifecycleService,
    install_certificate_schema,
)
from app.modules.certificates.http import build_router as build_certificates_router
from app.modules.secrets import SecretsCatalogService, build_router as build_secrets_router
from app.modules.hosts import build_inventory_router as build_host_inventory_router, build_management_router, build_router as build_host_router
from app.modules.clusters import build_settings_router
from app.modules.clusters.http import build_inventory_router, build_log_monitoring_router, build_membership_router, build_zoning_router
from app.modules.versions import VersionTargetInput
from app.modules.versions.http import build_router as build_versions_router
from app.modules.workloads.http import build_legacy_compatibility_router, build_mutation_router, build_router as build_workloads_router

PERSISTENT_DATA_DIR = Path("/var/lib/elastic-control")


def app_data_dir():
    """Prefer the controller's mounted data volume over a legacy relative path."""
    return platform_app_data_dir(PERSISTENT_DATA_DIR)


DATA = app_data_dir()
DB = DATA / "control.db"
RUNS = DATA / "runs"
INVENTORIES = DATA / "inventory"
VARIABLES = DATA / "variables"
SOURCE_ROOT = Path(__file__).resolve().parent.parent
KEY = os.getenv("APP_SECRET_KEY", "")
ADMIN = os.getenv("ADMIN_USERNAME", "admin")
PASSWORD = os.getenv("ADMIN_PASSWORD", "")
SSH_KEY = os.getenv("SSH_KEY_PATH", "/run/secrets/managed_nodes_ssh_key")
SSH_KNOWN_HOSTS = os.getenv("SSH_KNOWN_HOSTS_PATH", "/run/secrets/managed_nodes_known_hosts")
PLAYBOOKS = Path(os.getenv("PLAYBOOKS_DIR", "/opt/elastic-control/ansible/playbooks"))
if not PLAYBOOKS.exists():
    PLAYBOOKS = SOURCE_ROOT / "ansible" / "playbooks"
STATIC_DIR = Path(os.getenv("APP_STATIC_DIR", "/opt/elastic-control/static"))
if not STATIC_DIR.exists():
    STATIC_DIR = SOURCE_ROOT / "frontend" / "dist"
    if not STATIC_DIR.exists():
        STATIC_DIR = SOURCE_ROOT / "static"
# This location is intentionally outside APP_DATA_DIR. Managed private keys are
# encrypted in SQLite and are only materialized in ephemeral controller runtime.
RUNTIME = Path(os.getenv("APP_RUNTIME_DIR", "/run/elastic-control"))
SSH_RUNTIME = RUNTIME / "ssh"

ROLE_SPECS = {
    "master": {"label": "Master", "ports": ("elasticsearch_http", "elasticsearch_transport")},
    "hot": {"label": "Hot data", "ports": ("elasticsearch_http", "elasticsearch_transport")},
    "warm": {"label": "Warm data", "ports": ("elasticsearch_http", "elasticsearch_transport")},
    "ml": {"label": "Machine learning", "ports": ("elasticsearch_http", "elasticsearch_transport")},
    "ingest": {"label": "Ingest", "ports": ("elasticsearch_http", "elasticsearch_transport")},
    "coordinating": {"label": "Coordinating", "ports": ("elasticsearch_http", "elasticsearch_transport")},
    "kibana": {"label": "Kibana", "ports": ("kibana",)},
    "fleet-server": {"label": "Fleet Server", "ports": ("fleet",)},
    "logstash": {"label": "Logstash", "ports": ("logstash_api",)},
    "elastic-agent": {"label": "Elastic Agent", "ports": ()},
}
PORT_FIELDS = ("elasticsearch_http", "elasticsearch_transport", "kibana", "fleet", "logstash_api")
DEFAULT_PORTS = {
    "elasticsearch_http": 9200,
    "elasticsearch_transport": 9300,
    "kibana": 5601,
    "fleet": 8220,
    "logstash_api": 9600,
}
ELASTICSEARCH_ROLES = ("master", "hot", "warm", "ml", "ingest", "coordinating")
ROLE_PORT_OFFSETS = {role: index for index, role in enumerate(ELASTICSEARCH_ROLES)}
PATH_BLOCKLIST = ("/etc", "/usr", "/bin", "/sbin", "/lib", "/lib64", "/boot", "/proc", "/sys", "/dev", "/run", "/tmp")
CPU_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")
MEMORY_RE = re.compile(r"^[1-9][0-9]*(?:\.[0-9]+)?[kKmMgGtT]$")
VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
NODE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
ZONE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
DEFAULT_STACK_VERSION = "8.19.0"
DEFAULT_DISPLAY_TIMEZONE = os.getenv("APP_DISPLAY_TIMEZONE", "Asia/Hong_Kong")
THEME_PALETTE = ("#0077CC", "#00A67E", "#D36014", "#A13DAD", "#B41F4A", "#5367C9", "#6B7D00", "#008C95")
VERSION_OBSERVATION_MAX_AGE = 900
REGISTRY_CACHE_SECONDS = 900
REGISTRY_TAG_PAGE_SIZE = 100
REGISTRY_TAG_PAGE_LIMIT = 20
REGISTRY_TAG_RESULT_LIMIT = 1000
REGISTRY_REQUEST_TIMEOUT = 45
REGISTRY_LISTING_TIMEOUT = 10
REGISTRY_CACHE = {}
ROLE_IMAGES = {
    "master": "elasticsearch/elasticsearch",
    "hot": "elasticsearch/elasticsearch",
    "warm": "elasticsearch/elasticsearch",
    "ml": "elasticsearch/elasticsearch",
    "ingest": "elasticsearch/elasticsearch",
    "coordinating": "elasticsearch/elasticsearch",
    "kibana": "kibana/kibana",
    "fleet-server": "beats/elastic-agent",
    "elastic-agent": "beats/elastic-agent",
    "logstash": "logstash/logstash",
}
METRICBEAT_IMAGE = "beats/metricbeat"
METRICBEAT_ROLES = frozenset(("master", "hot", "warm", "ml", "ingest", "coordinating", "kibana", "logstash"))
FILEBEAT_IMAGE = "beats/filebeat"
FILEBEAT_RETENTION_DAYS = 30
UPGRADE_ORDER = ("warm", "hot", "ml", "ingest", "coordinating", "master", "kibana", "fleet-server", "logstash", "elastic-agent")
WORKLOAD_DEPLOY_ORDER = ("master", "hot", "warm", "ml", "ingest", "coordinating", "kibana", "fleet-server", "logstash", "elastic-agent")
ZONING_RECONCILE_ORDER = ("hot", "warm", "ml", "ingest", "coordinating", "master")


def environment_flag(name, default=False):
    return platform_environment_flag(name, default)


Login = LoginPayload


ControllerPassword = ControllerPasswordInput


ControllerKeyImport = ControllerKeyImportInput


@contextmanager
def db():
    with platform_db_connect(DB) as connection:
        yield connection


def digest(value, salt=None):
    return platform_digest(value, salt)


def valid_password(value, stored):
    return platform_valid_password(value, stored)


def config_cipher():
    key = base64.urlsafe_b64encode(hashlib.sha256(KEY.encode()).digest())
    return Fernet(key)


def seal_config(value):
    return platform_seal_config(value, KEY)


def open_config(value):
    return platform_open_config(value, KEY)


def seal_secret(value):
    return platform_seal_secret(value, KEY)


def open_secret(value):
    try:
        return platform_open_secret(value, KEY)
    except StoredSecretError as error:
        raise HTTPException(500, "Stored controller credential could not be decrypted") from error


def audit_event(username, action, item_id="", detail=""):
    write_event(db, username, action, item_id, detail)


def verify_current_password(username, password):
    if not platform_password_matches(db, username, password, valid_password):
        raise HTTPException(401, "Current administrator password is required")


def controller_settings():
    return {"timezone": platform_get_setting(db, "timezone", DEFAULT_DISPLAY_TIMEZONE)}


def secure_transport(request):
    """Compatibility hook for deployments that intentionally allow HTTP enrollment."""
    return None


def _controller_identity_operations():
    return ControllerIdentityOperations(
        db_factory=db,
        runtime_dir=SSH_RUNTIME,
        legacy_key_path=SSH_KEY,
        legacy_known_hosts_path=SSH_KNOWN_HOSTS,
        seal_secret=seal_secret,
        open_secret=open_secret,
        audit=lambda username, action, item_id, detail: audit_event(username, action, item_id, detail),
    )


def public_key_fingerprint(public_key):
    try:
        return identity_fingerprint(public_key)
    except ValueError as error:
        raise HTTPException(422, "Invalid OpenSSH public key") from error


def key_metadata(row):
    return _controller_identity_operations().key_metadata(row)


def legacy_key_metadata():
    return _controller_identity_operations().legacy_key_metadata()


def controller_key_rows():
    return _controller_identity_operations().key_rows()


def managed_key_path(row):
    return _controller_identity_operations().managed_key_path(row)


def remove_managed_key_path(key_id):
    return _controller_identity_operations().remove_managed_key_path(key_id)


def active_ssh_key_path():
    return _controller_identity_operations().active_ssh_key_path()


def enrollment_key_row():
    return _controller_identity_operations().enrollment_key_row()


def controller_key_status():
    return _controller_identity_operations().status()


def stage_controller_key(private_key, source):
    return _controller_identity_operations().stage(private_key, source)


def candidate_activation_status():
    return _controller_identity_operations().candidate_activation()


def activate_staged_controller_key(active, candidate, username):
    return _controller_identity_operations().activate(active, candidate, username)


def known_hosts_path(node_ids=None, include_legacy=True):
    return _controller_identity_operations().known_hosts_path(node_ids, include_legacy)


def redacted_config(value):
    return platform_redact_config(value)


def token_piece(value):
    return platform_token_piece(value)


def read_token_piece(value):
    return platform_read_token_piece(value)


def signed_token(username):
    return platform_signed_token(username, key=KEY)


def token_user(token):
    return platform_token_user(token, key=KEY)


def run_events_token(run_id):
    return platform_run_events_token(run_id, key=KEY)


def valid_run_events_token(token, run_id):
    return platform_valid_run_events_token(token, run_id, key=KEY)


def slugify(value):
    return value.lower().replace(".", "-").replace("_", "-")


def next_theme_color(con):
    return ClusterRepository.from_connection(con).next_theme_color_in_connection(con, THEME_PALETTE)


def valid_ipv4(value):
    return cluster_valid_ipv4(value)


def version_key(value):
    return versions_version_key(value)


def image_version(image):
    return versions_image_version(image)


def image_for_role(role, version):
    return versions_image_for_role(role, version)


def metricbeat_image(version):
    return f"docker.elastic.co/{METRICBEAT_IMAGE}:{version}"


def filebeat_image(version):
    return f"docker.elastic.co/{FILEBEAT_IMAGE}:{version}"


def log_monitoring_config(value, default_enabled=False):
    try:
        stored = json.loads(value or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        stored = {}
    return {
        "filebeat_enabled": bool(stored.get("filebeat_enabled", default_enabled)),
        "retention_days": FILEBEAT_RETENTION_DAYS,
    }


def workload_name(cluster, assignment):
    return f"ecp-{cluster['slug']}-{assignment['role']}-{assignment['node_id']}"


def _registry_client():
    return ElasticRegistry(
        cache=REGISTRY_CACHE, cache_seconds=REGISTRY_CACHE_SECONDS,
        request_timeout=REGISTRY_REQUEST_TIMEOUT, listing_timeout=REGISTRY_LISTING_TIMEOUT,
        tag_page_size=REGISTRY_TAG_PAGE_SIZE, tag_page_limit=REGISTRY_TAG_PAGE_LIMIT,
        tag_result_limit=REGISTRY_TAG_RESULT_LIMIT,
    )


def registry_json(url, headers=None):
    return _registry_client().json(url, headers)


def registry_tags(repository, cursor):
    return _registry_client().tags(repository, cursor, fetch_json=registry_json)


def registry_listing_tags(repository):
    return _registry_client().listing_tags(repository)


def registry_manifest_digest(repository, tag):
    return _registry_client().manifest_digest(repository, tag)


def cluster_repositories(assignments, filebeat_enabled=False):
    return registry_repositories(
        assignments, role_images=ROLE_IMAGES, metricbeat_roles=METRICBEAT_ROLES,
        metricbeat_image=METRICBEAT_IMAGE, filebeat_enabled=filebeat_enabled,
        filebeat_image=FILEBEAT_IMAGE,
    )


def registry_version_cursor(assignments):
    return registry_version_cursor_value(assignments, DEFAULT_STACK_VERSION)


def available_role_versions(role, assignments):
    return registry_available_role_versions(
        role, assignments, role_images=ROLE_IMAGES, default_version=DEFAULT_STACK_VERSION,
        listing_tags=registry_listing_tags, limit=REGISTRY_TAG_RESULT_LIMIT,
    )


def available_versions(assignments, filebeat_enabled=False):
    return registry_available_versions(
        assignments, role_images=ROLE_IMAGES, metricbeat_roles=METRICBEAT_ROLES,
        metricbeat_image=METRICBEAT_IMAGE, filebeat_image=FILEBEAT_IMAGE,
        default_version=DEFAULT_STACK_VERSION, registry_tags=registry_tags,
        result_limit=REGISTRY_TAG_RESULT_LIMIT, filebeat_enabled=filebeat_enabled,
    )


def target_image_digests(assignments, target_version):
    """Resolve one immutable registry digest per assignment image.

    This read-only registry lookup is deliberately separate from download and
    upgrade execution.  A maintenance plan must never persist a mutable tag as
    its target identity.
    """

    resolved = {}
    by_image = {}
    for assignment in assignments:
        image = image_for_role(assignment["role"], target_version)
        repository, tag = image.removeprefix("docker.elastic.co/").rsplit(":", 1)
        by_image.setdefault((repository, tag), []).append(int(assignment["id"]))
    for (repository, tag), assignment_ids in by_image.items():
        digest = registry_manifest_digest(repository, tag)
        for assignment_id in assignment_ids:
            resolved[assignment_id] = digest
    return resolved


def recommended_workload_version(assignments, candidates):
    return registry_recommended_version(assignments, candidates)


def observation_is_fresh(observation):
    return versions_observation_is_fresh(observation, VERSION_OBSERVATION_MAX_AGE)


def validate_membership_network(input):
    try:
        cluster_validate_membership_network(input)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


def membership_ready(member):
    return cluster_membership_ready(member)


membership_operations = MembershipOperations(
    cluster_repository=ClusterRepository,
    host_repository=HostRepository,
    workload_repository=WorkloadRepository,
)


def stored_zoning(value):
    return membership_operations.stored_zoning(value)


def require_cluster_host_zone(cluster, member):
    return membership_operations.require_cluster_host_zone(cluster, member)


def membership_node_record(con, node_id):
    return membership_operations.node_record(con, node_id)


def insert_membership(con, cluster_id, input):
    return membership_operations.insert(con, cluster_id, input)


def update_membership(con, cluster_id, node_id, input):
    return membership_operations.update(con, cluster_id, node_id, input)


def membership_has_assignments(con, cluster_id, node_id):
    return membership_operations.has_assignments(con, cluster_id, node_id)


def delete_membership(con, cluster_id, node_id):
    return membership_operations.delete(con, cluster_id, node_id)


def validate_zoning_catalog_update(con, cluster_id, zoning):
    return cluster_policy_service().validate_zoning_catalog_update(con, cluster_id, zoning)


def validate_host_zone_change(con, node_id, cluster_id, zone_id):
    return cluster_policy_service().validate_host_zone_change(con, node_id, cluster_id, zone_id)


def require_ready_membership(member):
    return membership_operations.require_ready(member)


def valid_storage_path(value):
    return workload_policy_service().valid_storage_path(value)


def validate_config(role, config):
    return workload_policy_service().validate_config(role, config)


def memory_mebibytes(value):
    return workload_policy_service().memory_mebibytes(value)


def workload_policy_service():
    return WorkloadPolicyService(
        role_specs=ROLE_SPECS,
        path_blocklist=PATH_BLOCKLIST,
        cpu_pattern=CPU_RE,
        memory_pattern=MEMORY_RE,
    )


def workload_service():
    return WorkloadService(db)


def workload_payload_service():
    return WorkloadPayloadService(
        cluster_repository=ClusterRepository,
        workload_repository=WorkloadRepository,
        host_repository=HostRepository,
        cluster_record=cluster_record,
        open_config=open_config,
        stored_role_ports=stored_role_ports,
        memory_mebibytes=memory_mebibytes,
        default_stack_version=DEFAULT_STACK_VERSION,
        elasticsearch_roles=ELASTICSEARCH_ROLES,
        require_ready_membership=require_ready_membership,
        require_cluster_host_zone=require_cluster_host_zone,
    )


def cluster_payload(con, row, desired_state="present", batch_assignment_ids=(), config_overrides=None):
    """Compatibility delegate for the workloads-owned payload service."""

    return workload_payload_service().build(
        con,
        row,
        desired_state,
        batch_assignment_ids=batch_assignment_ids,
        config_overrides=config_overrides,
    )


def profile_conflict(con, cluster_id, role_ports):
    return cluster_policy_service().profile_conflict(con, cluster_id, role_ports)


def stored_provider_profile(record):
    try:
        return provider_profile_from_record(record)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise HTTPException(409, "Cluster provider metadata is invalid and must be repaired before mutation") from error


def cluster_policy_service():
    return ClusterPolicyService(
        cluster_repository=ClusterRepository,
        workload_repository=WorkloadRepository,
        host_repository=HostRepository,
        stored_zoning=stored_zoning,
        role_port_values=role_port_values,
        stored_role_ports=stored_role_ports,
        role_specs=ROLE_SPECS,
        active_cluster_operation=active_cluster_operation,
        provider_profile_from_record=stored_provider_profile,
        require_capability=require_capability,
    )


def require_cluster_capability(con, cluster_id, capability):
    return cluster_policy_service().require_capability(con, cluster_id, capability)


def provider_profile_payload(profile, expected_cluster_uuid=None):
    return {
        "provider_type": profile.provider_type.value,
        "ownership_state": profile.ownership_state.value,
        "maintenance_backend": profile.maintenance_backend.value,
        "capability_overrides": dict(profile.capability_overrides),
        "capabilities": profile.capabilities.model_dump(),
        "connection_references": dict(profile.connection_references),
        "expected_cluster_uuid": expected_cluster_uuid,
        "revision": profile.revision,
    }


def cluster_record(con, cluster_id):
    return ClusterProjectionService(con).record(
        cluster_id,
        default_theme_color=THEME_PALETTE[0],
        default_version=DEFAULT_STACK_VERSION,
        stored_role_ports=stored_role_ports,
        stored_zoning=stored_zoning,
        log_monitoring_config=log_monitoring_config,
        stored_provider_profile=stored_provider_profile,
        provider_payload=provider_profile_payload,
        open_config=open_config,
        redacted_config=redacted_config,
        workload_maintenance_progress=workload_maintenance_progress_in_connection,
    )


def assignment_record(con, assignment_id):
    return WorkloadProjectionService(con).assignment_record(assignment_id)


def conflict_message(con, cluster_id, node_id, role):
    if not ROLE_SPECS[role]["ports"]:
        return None
    workloads = WorkloadRepository.from_connection(con)
    clusters = ClusterRepository.from_connection(con)
    rows = workloads.active_or_applying_for_node_outside_cluster_in_connection(con, node_id, cluster_id)
    target = clusters.record_in_connection(con, cluster_id)
    if not target:
        raise HTTPException(404, "Cluster not found")
    target_ports = stored_role_ports(target["role_ports_json"], json.loads(target["ports_json"]))
    used = set(role_port_values(target_ports, role))
    for row in rows:
        other_cluster = clusters.record_in_connection(con, int(row["cluster_id"]))
        if not other_cluster:
            continue
        other = stored_role_ports(other_cluster["role_ports_json"], json.loads(other_cluster["ports_json"]))
        if used.intersection(role_port_values(other, row["role"])):
            return f"Port profile conflicts with {other_cluster['name']} on this host"
    return None


async def user(request: Request):
    """Compatibility dependency retained for existing route declarations."""
    return await platform_require_user(request)


def init():
    if not KEY or not PASSWORD:
        raise RuntimeError("APP_SECRET_KEY and ADMIN_PASSWORD are required")
    paths = RuntimePaths.from_environment()
    # Older controller releases wrote materialized private keys below the
    # persistent data volume. The encrypted database record is authoritative.
    old_runtime_keys = DATA / "runtime" / "ssh"
    if old_runtime_keys != SSH_RUNTIME and old_runtime_keys.exists():
        for path in old_runtime_keys.glob("*.key"):
            path.unlink(missing_ok=True)

    workload_repository = WorkloadRepository

    def complete(connection):
        repository = workload_repository.from_connection(connection)
        return complete_controller_bootstrap(
            connection,
            maintenance_migrations=(install_maintenance_schema, install_certificate_schema),
            prepare_maintenance_recovery=MaintenanceStore(connection).prepare_startup_recovery,
            set_workload_batch_phase=repository.set_batch_phase,
            mark_recovery_required=platform_mark_recovery_required_in_connection,
            finish_run=platform_finish_run_in_connection,
            administrator=ADMIN,
            password_hash=digest(PASSWORD),
            default_timezone=DEFAULT_DISPLAY_TIMEZONE,
        )

    ControllerBootstrapService(
        runtime_paths=paths,
        database=db,
        bootstrap_schema=bootstrap_controller_schema,
        apply_schema_upgrades=lambda connection: apply_controller_schema_upgrades(
            connection,
            default_stack_version=DEFAULT_STACK_VERSION,
            theme_palette=THEME_PALETTE,
            network_defaults_json=NetworkDefaults().model_dump_json(),
            elasticsearch_settings_json=ElasticsearchSettings().model_dump_json(),
            stored_role_ports=stored_role_ports,
            log_monitoring_config=log_monitoring_config,
            open_config=open_config,
            seal_config=seal_config,
            token_factory=secrets.token_hex,
        ),
        complete_bootstrap=complete,
    ).run()


def _host_enrollment_orchestrator():
    return _host_operations().orchestrator()


def _host_operations():
    return HostOperations(
        orchestrator_type=HostEnrollmentOrchestrator,
        db_factory=db, inventories=INVENTORIES, runtime=RUNTIME, playbooks=PLAYBOOKS,
        active_key_path=active_ssh_key_path, enrollment_key=enrollment_key_row,
        managed_key_path=managed_key_path, known_hosts_path=known_hosts_path,
        launch=launch, audit=audit_event,
    )


def inventory(run_id, private_key=None, node_ids=None, password_bootstrap=False, pinned_host_key_only=False):
    return _host_enrollment_orchestrator().inventory(
        run_id, private_key, node_ids, password_bootstrap, pinned_host_key_only
    )


def password_test_known_hosts(node):
    return _host_enrollment_orchestrator().password_test_known_hosts(node)


def password_test_command(node, password_fd, known_hosts):
    return _host_enrollment_orchestrator().password_test_command(node, password_fd, known_hosts)


def verify_ssh_password(node, password):
    return _host_enrollment_orchestrator().verify_ssh_password(node, password)


def test_ssh_password(node, password):
    # Keep this compatibility seam patchable by callers while the concrete
    # password-only SSH execution belongs to the host orchestration module.
    authenticated, message = verify_ssh_password(node, password)
    if authenticated:
        return True, message
    if message != "Password authentication could not be started by the controller.":
        return False, "Password authentication failed. Check the SSH user, password, host key, and host reachability."
    return False, message


def add_log(run_id, value):
    platform_append_log(db, run_id, value)


def completed_run(kind, target, message, context=None):
    return platform_completed_run(db, kind, target, message, context)


def stream_command(command, on_line):
    """Compatibility seam for the module-owned streamed command executor."""

    return orchestration_stream_command(command, on_line)


def run_lifecycle_service():
    return RunLifecycleService(
        db_factory=db,
        stream_command=stream_command,
        add_log=add_log,
        append_log_in_connection=platform_append_log_in_connection,
        context_and_log=platform_context_and_log_in_connection,
        finish_run=platform_finish_run_in_connection,
        workload_repository=WorkloadRepository,
        host_repository=HostRepository,
        identity_repository=ControllerIdentityRepository,
        open_config=open_config,
        seal_config=seal_config,
        unique_node_name=unique_node_name,
        enrollment_hostname=enrollment_hostname,
        write_event=write_event_in_connection,
        launch_filebeat=lambda cluster_id, username: launch_filebeat_reconcile(cluster_id, username),
        http_exception_type=HTTPException,
    )


def platform_run_operations():
    """Build the platform run facade while preserving legacy callback seams."""
    return PlatformRunOperations(
        db_factory=db,
        variables_dir=VARIABLES,
        inventory_factory=inventory,
        run_descriptor=PlatformRunDescriptor,
        create_run=platform_create_run_in_connection,
        start_run=platform_start_run_in_connection,
        set_running_command=platform_set_running_command_in_connection,
        finish_run=platform_finish_run_in_connection,
        redacted_command=orchestration_redacted_command,
        lifecycle_service=run_lifecycle_service,
        run_execute=lambda run_id, command, temporary_paths: run_lifecycle_service().execute(
            run_id, command, temporary_paths
        ),
        add_log=add_log,
        stream_command=stream_command,
        schedule=asyncio.create_task,
    )


async def run(run_id, command, temporary_paths=()):
    return await platform_run_operations().run(run_id, command, temporary_paths)


def launch(kind, target, factory, variables=None, context=None, inventory_nodes=None, private_key=None, password_bootstrap=False, pinned_host_key_only=False):
    operations = platform_run_operations()
    return operations.launch(
        kind,
        target,
        factory,
        variables=variables,
        context=context,
        inventory_nodes=inventory_nodes,
        private_key=private_key,
        password_bootstrap=password_bootstrap,
        pinned_host_key_only=pinned_host_key_only,
    )


async def run_commands(run_id, commands, result_handler=None, temporary_paths=()):
    await platform_run_operations().run_commands(
        run_id,
        commands,
        result_handler=result_handler,
        temporary_paths=temporary_paths,
    )


def launch_commands(kind, target, factory, result_handler=None, context=None):
    return platform_run_operations().launch_commands(
        kind,
        target,
        factory,
        result_handler=result_handler,
        context=context,
    )


def probe_command(inv, cluster, assignment):
    return _version_operations().probe_command(inv, cluster, assignment)


def record_observation(metadata, output, succeeded):
    return _version_operations().record_observation(metadata, output, succeeded)


def download_command(inv, node_name, image):
    return _version_operations().download_command(inv, node_name, image)


def _version_operations():
    return VersionOperations(
        ansible=ansible,
        workload_name=workload_name,
        image_for_role=image_for_role,
        image_version=image_version,
        default_stack_version=DEFAULT_STACK_VERSION,
        repository_factory=lambda: VersionRepository(db),
        cluster_record=cluster_record,
        available_versions=available_versions,
        version_key=version_key,
        membership_ready=membership_ready,
        observation_is_fresh=observation_is_fresh,
        topology_elasticsearch_roles=TOPOLOGY_ES_ROLES,
        db_factory=db,
        variables_dir=VARIABLES,
        assignment_record=assignment_record,
        cluster_payload=cluster_payload,
        reconcile_command=reconcile_command,
        upgrade_preflight_command=upgrade_preflight_command,
        execute_logged_command=execute_logged_command,
        add_log=add_log,
        platform_finish_run=platform_finish_run_in_connection,
        workload_repository=WorkloadRepository,
        launch_filebeat_reconcile=launch_filebeat_reconcile,
        active_operation=lambda connection, cluster_name: platform_has_active_target_in_connection(connection, cluster_name + ":"),
        upgrade_order=UPGRADE_ORDER,
        start_run=platform_start_run_in_connection,
        run_descriptor=PlatformRunDescriptor,
        inventory=inventory,
    )


def version_details(con, cluster_id, include_candidates=True):
    return _version_operations().details(con, cluster_id, include_candidates=include_candidates)


def validate_version_target(cluster, target_version, candidates=None):
    return _version_operations().validate_target(cluster, target_version, candidates)


def upgrade_preflight(cluster, target_version, candidates=None):
    return _version_operations().preflight(cluster, target_version, candidates)


async def execute_logged_command(run_id, command):
    return await platform_execute_logged_command(
        run_id,
        command,
        add_log=add_log,
        stream_command=stream_command,
    )


def upgrade_preflight_command(inv, variables_path, node_name):
    return orchestration_playbook(inv, PLAYBOOKS / "cluster-upgrade-preflight.yml", node_name, active_ssh_key_path(), variables_path)


async def run_upgrade(run_id, cluster_id, target_version, inventory_path, assignment_ids):
    await _version_operations().run_upgrade(run_id, cluster_id, target_version, inventory_path, assignment_ids)


def launch_upgrade(cluster_id, target_version, candidates=None):
    return _version_operations().launch_upgrade(cluster_id, target_version, candidates)


def plan_maintenance_upgrade(connection, cluster, target_version, candidates, *, requested_by):
    """Compatibility adapter for the legacy upgrade route.

    Phase 4 is planning-only in this release.  The maintenance owner records
    the immutable target and a run for the established action-console flow;
    it does not invoke ``launch_upgrade`` or any remote mutation.
    """

    return MaintenanceUpgradePlanningService(
        MaintenanceStore(connection),
        image_for_role=image_for_role,
        resolve_target_digests=target_image_digests,
        preflight=upgrade_preflight,
        upgrade_order=UPGRADE_ORDER,
        execution_enabled=MAINTENANCE_CAPABILITIES["upgrade"],
    ).create_legacy_upgrade_plan(
        cluster,
        target_version=target_version,
        candidates=candidates,
        requested_by=requested_by,
    )


def workload_change_sort_key(change):
    return (WORKLOAD_DEPLOY_ORDER.index(change["role"]), change["node_name"], change["assignment_id"])


def active_cluster_operation(con, cluster_name):
    clusters = ClusterRepository.from_connection(con)
    cluster_id = clusters.id_for_name_in_connection(con, cluster_name)
    if platform_has_active_target_in_connection(con, cluster_name):
        return True
    return bool(
        cluster_id is not None
        and platform_any_active_ids_in_connection(
            con,
            WorkloadRepository.from_connection(con).operation_run_ids_for_cluster_in_connection(con, cluster_id),
        )
    )


def maintenance_conflicts(con, *, cluster_id=None, node_id=None, assignment_id=None):
    return MaintenanceReadRepository.from_connection(con).has_scope_conflict_in_connection(
        con,
        cluster_id=cluster_id,
        node_id=node_id,
        assignment_id=assignment_id,
    )


def require_no_maintenance_conflict(con, **scope):
    if maintenance_conflicts(con, **scope):
        raise HTTPException(409, "An active maintenance operation covers this host, cluster, or workload")


def active_assignments_for_change_set(con, cluster_id):
    return WorkloadProjectionService(con).active_change_set_records(cluster_id)


def validate_final_workload_ports(cluster, assignments):
    by_node = {}
    for assignment in assignments:
        by_node.setdefault(assignment["node_id"], []).append(assignment)
    for node_assignments in by_node.values():
        for index, left in enumerate(node_assignments):
            left_ports = set(role_port_values(cluster["role_ports"], left["role"]))
            for right in node_assignments[index + 1:]:
                if left_ports.intersection(role_port_values(cluster["role_ports"], right["role"])):
                    raise HTTPException(409, f"{ROLE_SPECS[left['role']]['label']} and {ROLE_SPECS[right['role']]['label']} use the same configured ports on {left['node_name']}")


def workload_change_validator():
    return WorkloadChangeValidator(
        cluster_record=cluster_record,
        active_operation=active_cluster_operation,
        active_assignments=active_assignments_for_change_set,
        validate_config=validate_config,
        recommended_version=recommended_workload_version,
        default_version=DEFAULT_STACK_VERSION,
        projection_factory=WorkloadProjectionService,
        require_ready_membership=require_ready_membership,
        require_cluster_host_zone=require_cluster_host_zone,
        elasticsearch_roles=ELASTICSEARCH_ROLES,
        conflict_message=conflict_message,
        open_config=open_config,
        validate_final_ports=validate_final_workload_ports,
    )


def validate_workload_change_set(con, cluster_id, input):
    return workload_change_validator().validate(con, cluster_id, input)


def batch_plan(con, run_id):
    return _workload_operations().batch_plan(con, run_id)


def record_batch_progress(con, run_id, completed):
    return _workload_operations().record_progress(con, run_id, completed)


async def execute_workload_change_reconcile(run_id, inv, payload, name, suffix):
    return await _workload_operations().execute_reconcile(run_id, inv, payload, name, suffix)


def workload_change_payload(con, item, plan, desired_state="present"):
    return _workload_operations().workload_payload(con, item, plan, desired_state)


async def rollback_workload_change_batch(run_id, inv, plan, completed):
    return await _workload_operations().rollback(run_id, inv, plan, completed)


def release_workload_change_batch(con, run_id, plan):
    return _workload_operations().release(con, run_id, plan)


async def recover_workload_change_batch(run_id):
    return await _workload_operations().recover(run_id)


async def recover_workload_change_batches():
    return await _workload_operations().recover_all()


async def run_workload_change_batch(run_id, inventory_path):
    return await _workload_operations().run_batch(
        run_id,
        inventory_path,
        reconcile_runner=execute_workload_change_reconcile,
    )


def _workload_operations():
    return WorkloadOperations(
        db_factory=db,
        variables_dir=VARIABLES,
        inventory_factory=inventory,
        workload_repository=WorkloadRepository,
        assignment_record=assignment_record,
        cluster_payload=cluster_payload,
        open_config=open_config,
        reconcile_command=reconcile_command,
        execute_logged_command=execute_logged_command,
        add_log=add_log,
        workload_sort_key=workload_change_sort_key,
        seal_config=seal_config,
        finish_run=platform_finish_run_in_connection,
        status_in_connection=platform_status_in_connection,
        launch_filebeat_reconcile=launch_filebeat_reconcile,
        recovery_required_ids=platform_recovery_required_ids_in_connection,
        start_run=platform_start_run_in_connection,
        run_descriptor=PlatformRunDescriptor,
        validate_change_set=validate_workload_change_set,
        schedule=asyncio.create_task,
    )


def launch_workload_change_batch(cluster_id, input):
    return _workload_operations().launch_batch(cluster_id, input)


def ansible(inv, target, module, args):
    return orchestration_module(inv, target, module, args, active_ssh_key_path())


def reconcile_command(inv, variables_path, name):
    return orchestration_playbook(inv, PLAYBOOKS / "cluster-reconcile.yml", name, active_ssh_key_path(), variables_path)


def zoning_settings_command(inv, variables_path, name):
    return orchestration_playbook(inv, PLAYBOOKS / "cluster-zoning-settings.yml", name, active_ssh_key_path(), variables_path)


def _zoning_operations():
    return ZoningOperations(
        db_factory=db,
        variables_dir=VARIABLES,
        cluster_record=cluster_record,
        assignment_record=assignment_record,
        cluster_payload=cluster_payload,
        cluster_repository=ClusterRepository,
        workload_repository=WorkloadRepository,
        host_repository=HostRepository,
        role_specs=ROLE_SPECS,
        elasticsearch_roles=ELASTICSEARCH_ROLES,
        zoning_reconcile_order=ZONING_RECONCILE_ORDER,
        active_cluster_operation=active_cluster_operation,
        require_cluster_host_zone=require_cluster_host_zone,
        reconcile_command=reconcile_command,
        zoning_settings_command=zoning_settings_command,
        execute_logged_command=execute_logged_command,
        add_log=add_log,
        append_log=platform_append_log_in_connection,
        finish_run=platform_finish_run_in_connection,
        start_run=platform_start_run_in_connection,
        completed_run=completed_run,
        open_config=open_config,
        inventory_factory=inventory,
    )


def zoning_assignments(cluster):
    return _zoning_operations().assignments(cluster)


def zoning_preflight(con, cluster_id):
    return _zoning_operations().preflight(con, cluster_id)


async def execute_zoning_reconcile(run_id, inv, payload, name, suffix):
    return await _zoning_operations().execute_reconcile(run_id, inv, payload, name, suffix)


async def execute_zoning_settings(run_id, inv, payload, name):
    return await _zoning_operations().execute_settings(run_id, inv, payload, name)


def zoning_settings_payload(con, cluster):
    return _zoning_operations().settings_payload(con, cluster)


async def rollback_zoning_reconciles(run_id, inv, completed, previous_zones):
    async def reconcile(run_id_, inv_, payload_, name_, suffix_):
        return await execute_zoning_reconcile(run_id_, inv_, payload_, name_, suffix_)
    return await _zoning_operations().rollback(run_id, inv, completed, previous_zones, reconcile=reconcile)


async def run_zoning_apply(run_id, cluster_id, inventory_path):
    async def reconcile(run_id_, inv_, payload_, name_, suffix_):
        return await execute_zoning_reconcile(run_id_, inv_, payload_, name_, suffix_)
    async def settings(run_id_, inv_, payload_, name_):
        return await execute_zoning_settings(run_id_, inv_, payload_, name_)
    return await _zoning_operations().run_apply(run_id, cluster_id, inventory_path, reconcile=reconcile, settings=settings)


def launch_zoning_apply(cluster_id):
    return _zoning_operations().launch_apply(cluster_id)


async def run_host_zone_change(run_id, node_id, previous_zone, zone_id, inventory_path):
    async def reconcile(run_id_, inv_, payload_, name_, suffix_):
        return await execute_zoning_reconcile(run_id_, inv_, payload_, name_, suffix_)
    return await _zoning_operations().run_host_zone_change(run_id, node_id, previous_zone, zone_id, inventory_path, reconcile=reconcile)


def filebeat_reconcile_command(inv, variables_path, name):
    return orchestration_playbook(inv, PLAYBOOKS / "filebeat-reconcile.yml", name, active_ssh_key_path(), variables_path)


def filebeat_payload(con, row):
    cluster = cluster_record(con, row["cluster_id"])
    payload = cluster_payload(con, row, "purge")
    payload["log_monitoring"] = cluster["log_monitoring"]
    payload["filebeat_image"] = filebeat_image(row["image_version"] or DEFAULT_STACK_VERSION)
    payload["filebeat_username"] = f"elkeeper_filebeat_{row['cluster_id']}"
    payload["filebeat_role"] = f"elkeeper_filebeat_writer_{row['cluster_id']}"
    return payload


async def execute_filebeat_reconcile(run_id, inv, payload, name, suffix):
    return await _filebeat_worker().execute(run_id, inv, payload, name, suffix)


def record_filebeat_observation(assignment_id, output, succeeded):
    return _filebeat_worker().record_observation(assignment_id, output, succeeded)


async def run_filebeat_reconcile(run_id, cluster_id, inventory_path):
    return await _filebeat_worker().run(run_id, cluster_id, inventory_path)


def _filebeat_worker():
    return FilebeatReconcileWorker(
        db_factory=db,
        variables_dir=VARIABLES,
        cluster_record=cluster_record,
        assignment_record=assignment_record,
        payload=filebeat_payload,
        command=filebeat_reconcile_command,
        stream_command=stream_command,
        add_log=add_log,
        repository_factory=lambda: VersionRepository(db),
        finish_run=platform_finish_run_in_connection,
        active_cluster_operation=active_cluster_operation,
        start_run=platform_start_run_in_connection,
        inventory_factory=inventory,
        audit_event=audit_event,
        run_descriptor=PlatformRunDescriptor,
        run_reconcile=lambda run_id, cluster_id, inventory_path: run_filebeat_reconcile(
            run_id, cluster_id, inventory_path
        ),
    )


def launch_filebeat_reconcile(cluster_id, username):
    return _filebeat_worker().launch(cluster_id, username)


async def start_telemetry():
    from app import console
    await console.telemetry.start()


async def stop_telemetry():
    from app import console
    await console.telemetry.stop()


life = build_lifespan(init, start_telemetry, recover_workload_change_batches, stop_telemetry)


app = FastAPI(title="Elastic Control Plane", lifespan=life)
mount_static_assets(app, STATIC_DIR)
install_security_headers(app)


def enrollment_variables(node, key, password=None, install_controller_key=True):
    return _host_enrollment_orchestrator().enrollment_variables(node, key, password, install_controller_key)


def enrollment_context(node_id, enabled, key_id="", install_controller_key=False, existing_key=False, auto_name=False, username=""):
    return _host_enrollment_orchestrator().enrollment_context(
        node_id, enabled, key_id, install_controller_key, existing_key, auto_name, username
    )


def launch_password_enrollment(node, password, install_controller_key, username, auto_name=False):
    return _host_enrollment_orchestrator().launch_password_enrollment(
        node, password, install_controller_key, username, auto_name
    )


def launch_key_enrollment_probe(node, username, auto_name=False):
    return _host_enrollment_orchestrator().launch_key_enrollment_probe(node, username, auto_name)


def cluster_lifecycle_service():
    return ClusterLifecycleService(
        db,
        slugify=slugify,
        seal_config=seal_config,
        token_factory=secrets.token_hex,
        log_monitoring_config=log_monitoring_config,
        palette=THEME_PALETTE,
        stored_provider_profile=stored_provider_profile,
        provider_payload=provider_profile_payload,
        require_no_maintenance_conflict=require_no_maintenance_conflict,
        require_cluster_capability=require_cluster_capability,
        cluster_settings_capability=ProviderCapability.CLUSTER_SETTINGS,
        profile_conflict=profile_conflict,
        validate_zoning_catalog_update=validate_zoning_catalog_update,
    )


def create_cluster_impl(input: ClusterInput):
    return cluster_lifecycle_service().create(input)


def get_cluster_provider_impl(cluster_id: int):
    return cluster_lifecycle_service().get_provider(cluster_id)


def update_cluster_provider_impl(
    cluster_id: int,
    input: ClusterProviderUpdate,
    username: str,
):
    return cluster_lifecycle_service().update_provider(cluster_id, input, username)


def update_cluster_impl(cluster_id: int, input: ClusterInput):
    return cluster_lifecycle_service().update(cluster_id, input)


TOPOLOGY_ES_ROLES = {
    "master": "master, remote_cluster_client",
    "hot": "data_hot, data_content, remote_cluster_client",
    "warm": "data_warm, data_content, remote_cluster_client",
    "ml": "ml, remote_cluster_client",
    "ingest": "ingest, remote_cluster_client",
    "coordinating": "coordinating only",
}


def delete_cluster_impl(cluster_id: int):
    from app import console
    cluster_lifecycle_service().delete(cluster_id, invalidate_cluster_ca=console.invalidate_cluster_ca)


def launch_cluster_settings(cluster, master, member, settings, credentials):
    payload = {
        "cluster": {
            "id": cluster["id"],
            "name": cluster["name"],
            "slug": cluster["slug"],
            "ports": cluster["ports"],
        },
        "bootstrap": {
            "node_name": master["node_name"],
            "node_id": master["node_id"],
            "user_address": member["user_address"],
        },
        "credentials": credentials,
        "settings": settings.model_dump(),
    }
    return launch(
        "cluster-settings",
        cluster["name"],
        lambda inventory_path, variables_path: orchestration_playbook(
            inventory_path,
            PLAYBOOKS / "cluster-settings.yml",
            master["node_name"],
            active_ssh_key_path(),
            variables_path,
        ),
        variables=payload,
    )


cluster_settings_service = ClusterSettingsService(
    db_factory=db,
    cluster_record=cluster_record,
    require_no_maintenance_conflict=require_no_maintenance_conflict,
    require_cluster_capability=require_cluster_capability,
    settings_capability=ProviderCapability.CLUSTER_SETTINGS,
    open_config=open_config,
    completed_run=completed_run,
    launch_settings=launch_cluster_settings,
)


from app import console

console.configure_runtime(
    console.ConsoleRuntimeDependencies(
        data=DATA,
        db_factory=db,
        secret_key=KEY,
        active_key_path=active_ssh_key_path,
        known_hosts_path=known_hosts_path,
        host_key_args=ssh_host_key_args,
        valid_storage_path=valid_storage_path,
        workload_name=workload_name,
        image_version=image_version,
        open_config=open_config,
        seal_config=seal_config,
        cluster_record=cluster_record,
        cluster_settings_service=lambda: cluster_settings_service,
        secrets_catalog_service=lambda: secrets_catalog_service,
    )
)

app.include_router(console.router)
app.include_router(
    build_cluster_lifecycle_router(
        cluster_input_model=ClusterInput,
        provider_update_model=ClusterProviderUpdate,
        create_cluster=create_cluster_impl,
        update_cluster=update_cluster_impl,
        delete_cluster=delete_cluster_impl,
        get_provider=get_cluster_provider_impl,
        update_provider=update_cluster_provider_impl,
        user_dependency=user,
    )
)


def _active_cluster_operation_for_router(cluster_name: str):
    with db() as connection:
        return active_cluster_operation(connection, cluster_name)


def _list_clusters_for_router():
    with db() as connection:
        ids = ClusterRepository(db).ids()
        return [cluster_record(connection, cluster_id) for cluster_id in ids]


def _get_cluster_for_router(cluster_id: int):
    with db() as connection:
        return cluster_record(connection, cluster_id)


secrets_catalog_service = SecretsCatalogService(
    cluster_provider=_get_cluster_for_router,
    encrypted_credentials_provider=ClusterRepository(db).secrets_json,
    host_provider=HostRepository(db).get,
    certificate_inventory=CertificateInventoryService(HostRepository(db).get),
)

certificate_lifecycle_service = CertificateLifecycleService(
    db_factory=db,
    cluster_provider=_get_cluster_for_router,
    audit_event=lambda username, action, cluster_id, item_id, detail: audit_cluster_event(
        username, action, cluster_id, item_id, json.dumps(detail, sort_keys=True)
    ),
    rolling_restart_capability=lambda: bool(MAINTENANCE_CAPABILITIES["rolling_restart"]),
    completed_run=lambda kind, target, message, context=None: completed_run(kind, target, message, context),
    node_provider=HostRepository(db).get,
    remote_file_reader=lambda node, path: console.remote_command(node, "cat", "--", path),
)


app.include_router(
    build_versions_router(
        db_factory=db,
        user_dependency=user,
        role_specs=ROLE_SPECS,
        cluster_record=lambda *args, **kwargs: cluster_record(*args, **kwargs),
        version_details=lambda *args, **kwargs: version_details(*args, **kwargs),
        available_role_versions=lambda *args, **kwargs: available_role_versions(*args, **kwargs),
        available_versions=lambda *args, **kwargs: available_versions(*args, **kwargs),
        recommended_version=lambda *args, **kwargs: recommended_workload_version(*args, **kwargs),
        validate_version_target=lambda *args, **kwargs: validate_version_target(*args, **kwargs),
        image_for_role=lambda *args, **kwargs: image_for_role(*args, **kwargs),
        metricbeat_roles=METRICBEAT_ROLES,
        metricbeat_image=lambda *args, **kwargs: metricbeat_image(*args, **kwargs),
        filebeat_enabled_image=lambda *args, **kwargs: filebeat_image(*args, **kwargs),
        launch_commands=lambda *args, **kwargs: launch_commands(*args, **kwargs),
        probe_command=lambda *args, **kwargs: probe_command(*args, **kwargs),
        record_observation=lambda *args, **kwargs: record_observation(*args, **kwargs),
        download_command=lambda *args, **kwargs: download_command(*args, **kwargs),
        plan_upgrade=lambda *args, **kwargs: plan_maintenance_upgrade(*args, **kwargs),
        active_operation_checker=lambda cluster_name: _active_cluster_operation_for_router(cluster_name),
        require_no_maintenance_conflict=lambda *args, **kwargs: require_no_maintenance_conflict(*args, **kwargs),
        require_cluster_capability=lambda *args, **kwargs: require_cluster_capability(*args, **kwargs),
        workload_mutation_capability=ProviderCapability.WORKLOAD_MUTATION,
        lifecycle_capability=ProviderCapability.LIFECYCLE_API,
        default_stack_version=DEFAULT_STACK_VERSION,
    )
)
app.include_router(
    build_workloads_router(
        db_factory=db,
        user_dependency=user,
        cluster_record=lambda *args, **kwargs: cluster_record(*args, **kwargs),
        render_topology=lambda *args, **kwargs: workload_render_topology(*args, **kwargs),
        role_specs=ROLE_SPECS,
        elasticsearch_roles=TOPOLOGY_ES_ROLES,
        valid_ipv4=valid_ipv4,
    )
)
app.include_router(
    build_mutation_router(
        db_factory=db,
        user_dependency=user,
        assignment_model=AssignmentInput,
        change_set_model=WorkloadChangeSet,
        resource_model=ResourceInput,
        membership_exists=lambda connection, cluster_id, node_id: ClusterRepository.from_connection(connection).membership_exists_in_connection(
            connection, cluster_id, node_id
        ),
        node_enabled=lambda connection, node_id: HostRepository.from_connection(connection).is_enabled_in_connection(
            connection, node_id
        ),
        validate_config=lambda *args, **kwargs: validate_config(*args, **kwargs),
        cluster_record=lambda *args, **kwargs: cluster_record(*args, **kwargs),
        require_no_maintenance_conflict=lambda *args, **kwargs: require_no_maintenance_conflict(*args, **kwargs),
        require_cluster_capability=lambda *args, **kwargs: require_cluster_capability(*args, **kwargs),
        workload_mutation_capability=ProviderCapability.WORKLOAD_MUTATION,
        conflict_message=lambda *args, **kwargs: conflict_message(*args, **kwargs),
        seal_config=lambda *args, **kwargs: seal_config(*args, **kwargs),
        assignment_record=lambda *args, **kwargs: assignment_record(*args, **kwargs),
        open_config=lambda *args, **kwargs: open_config(*args, **kwargs),
        require_ready_membership=lambda *args, **kwargs: require_ready_membership(*args, **kwargs),
        require_initial_master_batch=lambda *args, **kwargs: workload_service().require_initial_master_batch(*args, **kwargs),
        cluster_payload=lambda *args, **kwargs: cluster_payload(*args, **kwargs),
        launch_workload_change_batch=lambda *args, **kwargs: launch_workload_change_batch(*args, **kwargs),
        launch=lambda *args, **kwargs: launch(*args, **kwargs),
        reconcile_command=lambda *args, **kwargs: reconcile_command(*args, **kwargs),
        repository_factory=WorkloadRepository.from_connection,
    )
)
app.include_router(build_legacy_compatibility_router(user_dependency=user))
app.include_router(
    build_membership_router(
        db_factory=db,
        user_dependency=user,
        membership_model=MembershipInput,
        validate_membership_network=lambda *args, **kwargs: validate_membership_network(*args, **kwargs),
        cluster_record=lambda *args, **kwargs: cluster_record(*args, **kwargs),
        require_no_maintenance_conflict=lambda *args, **kwargs: require_no_maintenance_conflict(*args, **kwargs),
        require_cluster_capability=lambda *args, **kwargs: require_cluster_capability(*args, **kwargs),
        workload_mutation_capability=ProviderCapability.WORKLOAD_MUTATION,
        require_cluster_host_zone=lambda *args, **kwargs: require_cluster_host_zone(*args, **kwargs),
        node_record=lambda *args, **kwargs: membership_node_record(*args, **kwargs),
        insert_membership=lambda *args, **kwargs: insert_membership(*args, **kwargs),
        update_membership=lambda *args, **kwargs: update_membership(*args, **kwargs),
        has_assignments=lambda *args, **kwargs: membership_has_assignments(*args, **kwargs),
        delete_membership=lambda *args, **kwargs: delete_membership(*args, **kwargs),
    )
)
app.include_router(
    build_log_monitoring_router(
        db_factory=db,
        user_dependency=user,
        input_model=LogMonitoringInput,
        cluster_record=lambda *args, **kwargs: cluster_record(*args, **kwargs),
        require_no_maintenance_conflict=lambda *args, **kwargs: require_no_maintenance_conflict(*args, **kwargs),
        require_cluster_capability=lambda *args, **kwargs: require_cluster_capability(*args, **kwargs),
        workload_mutation_capability=ProviderCapability.WORKLOAD_MUTATION,
        active_cluster_operation=lambda *args, **kwargs: active_cluster_operation(*args, **kwargs),
        retention_days=FILEBEAT_RETENTION_DAYS,
        audit_event=lambda *args, **kwargs: audit_event(*args, **kwargs),
        launch_reconcile=lambda *args, **kwargs: launch_filebeat_reconcile(*args, **kwargs),
        update_observability=lambda connection, cluster_id, value: ClusterRepository.from_connection(
            connection
        ).update_observability_in_connection(connection, cluster_id, value),
    )
)
app.include_router(
    build_zoning_router(
        db_factory=db,
        user_dependency=user,
        zoning_model=ZoningConfig,
        cluster_record=lambda *args, **kwargs: cluster_record(*args, **kwargs),
        require_no_maintenance_conflict=lambda *args, **kwargs: require_no_maintenance_conflict(*args, **kwargs),
        require_cluster_capability=lambda *args, **kwargs: require_cluster_capability(*args, **kwargs),
        cluster_settings_capability=ProviderCapability.CLUSTER_SETTINGS,
        validate_catalog_update=lambda *args, **kwargs: validate_zoning_catalog_update(*args, **kwargs),
        update_zoning=lambda connection, cluster_id, zoning_json: ClusterRepository.from_connection(connection).update_zoning_in_connection(
            connection, cluster_id, zoning_json
        ),
        audit_event=lambda username, action, cluster_id, item_id, detail: audit_cluster_event(
            username, action, cluster_id, item_id, detail
        ),
        completed_run=lambda *args, **kwargs: completed_run(*args, **kwargs),
        launch_apply=lambda *args, **kwargs: launch_zoning_apply(*args, **kwargs),
    )
)
app.include_router(
    build_inventory_router(
        list_clusters=lambda: _list_clusters_for_router(),
        get_cluster=lambda cluster_id: _get_cluster_for_router(cluster_id),
        user_dependency=user,
    )
)

app.include_router(
    build_observability_router(
        db_factory=db,
        telemetry_provider=lambda: console.telemetry,
        signed_scope_token=console.signed_scope_token,
        valid_scope_token=console.valid_scope_token,
        token_user=token_user,
        user_dependency=user,
        host_provider=HostRepository(db).get,
        observation_provider=lambda node_id: runtime_observation(db, node_id),
    )
)
def sensitive_catalog_for_cluster(cluster_id):
    return secrets_catalog_service.catalog(cluster_id)


def audit_sensitive(username, action, cluster_id, item_id=""):
    with db() as connection:
        write_event_in_connection(connection, username, action, cluster_id=cluster_id, item_id=item_id)


def audit_cluster_event(username, action, cluster_id, item_id, detail):
    with db() as connection:
        write_event_in_connection(
            connection,
            username,
            action,
            cluster_id=cluster_id,
            item_id=item_id,
            detail=detail,
        )


app.include_router(
    build_secrets_router(
        catalog_provider=sensitive_catalog_for_cluster,
        metadata_provider=lambda item: console.remote_sensitive_metadata(item),
        read_remote=lambda node, *command: console.remote_command(node, *command),
        verify_reauthentication=verify_current_password,
        audit_fn=audit_sensitive,
        user_dependency=user,
    )
)
app.include_router(build_certificates_router(service=certificate_lifecycle_service, user_dependency=user))
app.include_router(
    build_host_router(
        host_provider=HostRepository(db).get,
        remote_command=lambda node, *command, **kwargs: console.remote_command(node, *command, **kwargs),
        storage_renderer=lambda payload: console.storage_mounts(payload),
        user_dependency=user,
    )
)
app.include_router(
    build_host_inventory_router(
        node_model=Node,
        list_nodes=lambda: HostRepository(db).list(),
        create_node=lambda payload: HostRepository(db).create(payload),
        user_dependency=user,
    )
)
app.include_router(
    build_management_router(
        password_test_model=NodePasswordTest,
        enrollment_model=NodeEnrollment,
        key_install_model=KeyInstall,
        node_update_model=NodeUpdate,
        zone_model=HostZoneInput,
        db_factory=db,
        normalize_host_key=normalize_ssh_host_key,
        password_test=lambda node, password: test_ssh_password(node, password),
        enrollment_key=enrollment_key_row,
        launch_password_enrollment=lambda node, password, install_key, username, auto_name: launch_password_enrollment(
            node, password, install_key, username, auto_name=auto_name
        ),
        launch_key_enrollment_probe=lambda node, username, auto_name: launch_key_enrollment_probe(
            node, username, auto_name=auto_name
        ),
        require_no_conflict=lambda connection, node_id: require_no_maintenance_conflict(connection, node_id=node_id),
        validate_zone_change=validate_host_zone_change,
        fingerprint=public_key_fingerprint,
        controller_key_rows=controller_key_rows,
        launch_key_revocation=lambda node, key, node_id: launch(
            "host-key-revoke",
            node["name"],
            lambda inventory_path, variables_path: orchestration_playbook(
                inventory_path,
                PLAYBOOKS / "host-revoke-controller-key.yml",
                node["name"],
                active_ssh_key_path(),
                variables_path,
            ),
            variables={"controller_public_key": key["public_key"]},
            context={"delete_node_after_revoke": node_id},
            inventory_nodes=[node_id],
        ),
        launch_probe=lambda name: launch("probe", name, lambda inventory_path, _variables: ansible(inventory_path, name, "ping", "")),
        completed_run=lambda kind, target, message, context=None: completed_run(kind, target, message, context),
        inventory_for_run=lambda run_id: inventory(run_id),
        run_zone_change=lambda run_id, node_id, previous, zone_id, inventory_path: run_host_zone_change(
            run_id, node_id, previous, zone_id, inventory_path
        ),
        audit_fn=lambda username, action, item_id, detail: audit_event(username, action, item_id, detail),
        has_membership=lambda connection, node_id: ClusterRepository.from_connection(connection).has_membership_for_node_in_connection(
            connection, node_id
        ),
        user_dependency=user,
    )
)


def _host_lifecycle_operations():
    """Assemble the public host lifecycle facade from runtime dependencies."""

    return HostLifecycleOperations(
        db_factory=db,
        require_no_maintenance_conflict=require_no_maintenance_conflict,
        workload_repository_type=WorkloadRepository,
        playbooks=PLAYBOOKS,
        active_key_path=lambda: active_ssh_key_path(),
        launch=lambda *args, **kwargs: launch(*args, **kwargs),
        playbook_command=lambda *args, **kwargs: orchestration_playbook(*args, **kwargs),
    )


def phase2_reboot_adapter_factory(**dependencies):
    """Expose inert Phase 2 reboot composition without registering an adapter."""

    return Phase2RebootAdapterFactory(**dependencies)


app.include_router(
    _host_lifecycle_operations().lifecycle_router(
        enabled_host_provider=console.enabled_node,
        user_dependency=user,
    )
)
app.include_router(
    _host_lifecycle_operations().batch_router(
        batch_model=Targets,
        user_dependency=user,
    )
)
app.include_router(
    build_settings_router(
        settings_model=ElasticsearchSettings,
        get_settings=cluster_settings_service.get,
        update_settings=cluster_settings_service.update,
        user_dependency=user,
    )
)
app.include_router(maintenance_router)
app.include_router(build_maintenance_router(MAINTENANCE_CAPABILITIES))
app.include_router(build_identity_router(db_factory=db, audit_fn=audit_event, default_timezone=DEFAULT_DISPLAY_TIMEZONE))
app.include_router(
    build_key_router(
        key_status=controller_key_status,
        verify_password=verify_current_password,
        generate_private_key=ed25519.Ed25519PrivateKey.generate,
        parse_private_key=parse_imported_private_key,
        stage_key=stage_controller_key,
        candidate_activation=candidate_activation_status,
        activate_key=activate_staged_controller_key,
        audit_fn=audit_event,
    )
)
app.include_router(
    build_router(
        role_specs=ROLE_SPECS,
        static_dir=STATIC_DIR,
        run_events_token=run_events_token,
        valid_run_events_token=valid_run_events_token,
        token_user_fn=token_user,
        password_matches=lambda username, password: platform_password_matches(db, username, password, valid_password),
        recent_runs=lambda limit: platform_recent_runs(db, limit),
        stream_events=lambda run_id: platform_stream_run_events(db, run_id),
        signed_token_fn=signed_token,
    )
)
