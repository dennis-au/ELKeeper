import asyncio
import base64
import binascii
import calendar
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
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
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PERSISTENT_DATA_DIR = Path("/var/lib/elastic-control")


def app_data_dir():
    """Prefer the controller's mounted data volume over a legacy relative path."""
    configured = Path(os.getenv("APP_DATA_DIR", str(PERSISTENT_DATA_DIR)))
    if configured.is_absolute() or not PERSISTENT_DATA_DIR.exists():
        return configured
    return PERSISTENT_DATA_DIR


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
DEFAULT_STACK_VERSION = "8.19.0"
DEFAULT_DISPLAY_TIMEZONE = os.getenv("APP_DISPLAY_TIMEZONE", "Asia/Hong_Kong")
THEME_PALETTE = ("#0077CC", "#00A67E", "#D36014", "#A13DAD", "#B41F4A", "#5367C9", "#6B7D00", "#008C95")
VERSION_OBSERVATION_MAX_AGE = 900
REGISTRY_CACHE_SECONDS = 900
REGISTRY_TAG_PAGE_SIZE = 100
REGISTRY_TAG_PAGE_LIMIT = 20
REGISTRY_TAG_RESULT_LIMIT = 10
REGISTRY_REQUEST_TIMEOUT = 45
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


class Login(BaseModel):
    username: str
    password: str


class Node(BaseModel):
    name: str = Field(pattern=NODE_NAME_RE.pattern)
    address: str = Field(min_length=1, max_length=255)
    ssh_port: int = Field(default=22, ge=1, le=65535)
    ssh_user: str = Field(default="root", pattern=r"^[A-Za-z_][A-Za-z0-9_-]{0,31}$")
    enabled: bool = True

    @field_validator("address")
    @classmethod
    def ssh_address_must_be_ip(cls, value):
        try:
            return str(ipaddress.ip_address(value.strip()))
        except ValueError as error:
            raise ValueError("SSH address must be an IPv4 or IPv6 address") from error


class ControllerPassword(BaseModel):
    password: str = Field(min_length=1, max_length=1024)


class ControllerSettingsInput(BaseModel):
    timezone: str = Field(min_length=1, max_length=64)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value):
        timezone = value.strip()
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError as error:
            raise ValueError("Choose a valid IANA timezone") from error
        return timezone


class ControllerKeyImport(ControllerPassword):
    private_key: str = Field(min_length=64, max_length=32768)
    passphrase: str | None = Field(default=None, max_length=1024)


class NodeEnrollment(Node):
    name: str = Field(default="", max_length=128)
    ssh_host_key: str = Field(default="", max_length=8192)
    auth_method: str = Field(default="controller_key", pattern=r"^(controller_key|password)$")
    password: str | None = Field(default=None, max_length=1024)
    install_controller_key: bool = True

    @model_validator(mode="after")
    def validate_credentials(self):
        if self.name and not NODE_NAME_RE.fullmatch(self.name):
            raise ValueError("Inventory name may contain letters, numbers, dots, underscores, and hyphens")
        if self.auth_method == "password" and not self.password:
            raise ValueError("A password is required for password bootstrap")
        if self.auth_method == "controller_key" and self.password:
            raise ValueError("Passwords may only be supplied for password bootstrap")
        return self


class NodePasswordTest(BaseModel):
    address: str = Field(min_length=1, max_length=255)
    ssh_port: int = Field(default=22, ge=1, le=65535)
    ssh_user: str = Field(default="root", pattern=r"^[A-Za-z_][A-Za-z0-9_-]{0,31}$")
    ssh_host_key: str = Field(default="", max_length=8192)
    password: str = Field(min_length=1, max_length=1024)

    @field_validator("address")
    @classmethod
    def ssh_address_must_be_ip(cls, value):
        try:
            return str(ipaddress.ip_address(value.strip()))
        except ValueError as error:
            raise ValueError("SSH address must be an IPv4 or IPv6 address") from error


class NodeUpdate(Node):
    ssh_host_key: str | None = Field(default=None, max_length=8192)


class KeyInstall(ControllerPassword):
    pass


class PortProfile(BaseModel):
    elasticsearch_http: int = DEFAULT_PORTS["elasticsearch_http"]
    elasticsearch_transport: int = DEFAULT_PORTS["elasticsearch_transport"]
    kibana: int = DEFAULT_PORTS["kibana"]
    fleet: int = DEFAULT_PORTS["fleet"]
    logstash_api: int = DEFAULT_PORTS["logstash_api"]

    @model_validator(mode="after")
    def unique_ports(self):
        values = [getattr(self, field) for field in PORT_FIELDS]
        if any(port < 1 or port > 65535 for port in values) or len(set(values)) != len(values):
            raise ValueError("Ports must be unique values from 1 through 65535")
        return self


def valid_port(value, fallback):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return fallback
    return number if 1 <= number <= 65535 else fallback


def next_available_port(preferred, used, fallback):
    for start in (valid_port(preferred, fallback), valid_port(fallback, 1), 1):
        for value in range(start, 65536):
            if value not in used:
                used.add(value)
                return value
    raise ValueError("No port is available for a role association")


def default_role_ports(legacy_ports=None):
    supplied = legacy_ports or {}
    legacy = {name: valid_port(supplied.get(name), value) for name, value in DEFAULT_PORTS.items()}
    result = {
        "kibana": {"kibana": legacy["kibana"]},
        "fleet-server": {"fleet": legacy["fleet"]},
        "logstash": {"logstash_api": legacy["logstash_api"]},
        "elastic-agent": {},
    }
    used = {legacy["kibana"], legacy["fleet"], legacy["logstash_api"]}
    for role, offset in ROLE_PORT_OFFSETS.items():
        result[role] = {
            "elasticsearch_http": next_available_port(legacy["elasticsearch_http"] + offset, used, 9200 + offset),
            "elasticsearch_transport": next_available_port(legacy["elasticsearch_transport"] + offset, used, 9300 + offset),
        }
    return result


class RolePortProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    master: dict[str, int] = Field(default_factory=lambda: default_role_ports()["master"])
    hot: dict[str, int] = Field(default_factory=lambda: default_role_ports()["hot"])
    warm: dict[str, int] = Field(default_factory=lambda: default_role_ports()["warm"])
    ml: dict[str, int] = Field(default_factory=lambda: default_role_ports()["ml"])
    ingest: dict[str, int] = Field(default_factory=lambda: default_role_ports()["ingest"])
    coordinating: dict[str, int] = Field(default_factory=lambda: default_role_ports()["coordinating"])
    kibana: dict[str, int] = Field(default_factory=lambda: default_role_ports()["kibana"])
    fleet_server: dict[str, int] = Field(default_factory=lambda: default_role_ports()["fleet-server"], alias="fleet-server")
    logstash: dict[str, int] = Field(default_factory=lambda: default_role_ports()["logstash"])
    elastic_agent: dict[str, int] = Field(default_factory=dict, alias="elastic-agent")

    @model_validator(mode="after")
    def valid_role_ports(self):
        profiles = self.model_dump(by_alias=True)
        seen = {}
        for role, spec in ROLE_SPECS.items():
            association = profiles[role]
            expected = set(spec["ports"])
            if set(association) != expected:
                raise ValueError(f"{spec['label']} must define exactly: {', '.join(spec['ports']) or 'no inbound ports'}")
            for port in association.values():
                if port < 1 or port > 65535:
                    raise ValueError("Role ports must be values from 1 through 65535")
                if port in seen:
                    raise ValueError(f"Role port {port} is assigned to both {seen[port]} and {spec['label']}")
                seen[port] = spec["label"]
        return self


def stored_role_ports(value, legacy_ports):
    try:
        parsed = json.loads(value or "{}")
        if parsed:
            return RolePortProfile.model_validate(parsed).model_dump(by_alias=True)
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    return default_role_ports(legacy_ports)


def role_port_values(role_ports, role):
    return [role_ports[role][field] for field in ROLE_SPECS[role]["ports"]]


class NetworkDefaults(BaseModel):
    mode: str = Field(default="shared", pattern=r"^(dedicated|shared)$")


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
    desired_version: str = Field(default=DEFAULT_STACK_VERSION, pattern=r"^\d+\.\d+\.\d+$")
    network_defaults: NetworkDefaults = Field(default_factory=NetworkDefaults)
    elasticsearch_settings: ElasticsearchSettings = Field(default_factory=ElasticsearchSettings)

    @model_validator(mode="before")
    @classmethod
    def derive_role_ports_from_legacy_ports(cls, value):
        """Keep older cluster clients' custom port profiles meaningful."""
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


class AssignmentInput(BaseModel):
    node_id: int = Field(ge=1)
    role: str
    config: dict = Field(default_factory=dict)


class ResourceInput(BaseModel):
    cpu: str = Field(min_length=1, max_length=32)
    memory: str = Field(min_length=2, max_length=32)
    storage_path: str = Field(min_length=2, max_length=512)


class WorkloadChange(BaseModel):
    client_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    kind: str = Field(pattern=r"^(create|resources|detach)$")
    assignment_id: int | None = Field(default=None, ge=1)
    expected_revision: int | None = Field(default=None, ge=1)
    node_id: int | None = Field(default=None, ge=1)
    role: str | None = None
    config: dict | None = None

    @model_validator(mode="after")
    def validate_shape(self):
        if self.kind == "create":
            if not self.node_id or not self.role or self.config is None:
                raise ValueError("A create change requires node_id, role, and config")
            if self.assignment_id or self.expected_revision:
                raise ValueError("A create change may not include an assignment revision")
        else:
            if not self.assignment_id or not self.expected_revision:
                raise ValueError("An existing workload change requires assignment_id and expected_revision")
            if self.node_id or self.role:
                raise ValueError("An existing workload change may not include node_id or role")
            if self.kind == "resources" and self.config is None:
                raise ValueError("A resource change requires config")
            if self.kind == "detach" and self.config is not None:
                raise ValueError("A detach change may not include config")
        return self


class WorkloadChangeSet(BaseModel):
    changes: list[WorkloadChange] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_changes(self):
        client_ids = [change.client_id for change in self.changes]
        if len(client_ids) != len(set(client_ids)):
            raise ValueError("Each pending change needs a unique client_id")
        assignment_ids = [change.assignment_id for change in self.changes if change.assignment_id]
        if len(assignment_ids) != len(set(assignment_ids)):
            raise ValueError("A workload can only appear once in a pending change set")
        creates = [(change.node_id, change.role) for change in self.changes if change.kind == "create"]
        if len(creates) != len(set(creates)):
            raise ValueError("A role can only be created once on the same host")
        return self


class VersionTargetInput(BaseModel):
    target_version: str = Field(pattern=r"^\d+\.\d+\.\d+$")


class Targets(BaseModel):
    node_ids: list[int] = Field(min_length=1)


@contextmanager
def db():
    connection = sqlite3.connect(DB)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def digest(value, salt=None):
    salt = salt or secrets.token_bytes(16)
    value = hashlib.scrypt(value.encode(), salt=salt, n=16384, r=8, p=1)
    return base64.urlsafe_b64encode(salt + value).decode()


def valid_password(value, stored):
    raw = base64.urlsafe_b64decode(stored)
    return hmac.compare_digest(digest(value, raw[:16]), stored)


def config_cipher():
    key = base64.urlsafe_b64encode(hashlib.sha256(KEY.encode()).digest())
    return Fernet(key)


def seal_config(value):
    return config_cipher().encrypt(value.encode()).decode()


def open_config(value):
    try:
        return json.loads(config_cipher().decrypt(value.encode()).decode())
    except (InvalidToken, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return json.loads(value)


def seal_secret(value):
    return config_cipher().encrypt(value.encode()).decode()


def open_secret(value):
    try:
        return config_cipher().decrypt(value.encode()).decode()
    except (InvalidToken, ValueError, UnicodeDecodeError) as error:
        raise HTTPException(500, "Stored controller credential could not be decrypted") from error


def audit_event(username, action, item_id="", detail=""):
    with db() as con:
        con.execute(
            "INSERT INTO audit_events(username,action,item_id,detail) VALUES (?,?,?,?)",
            (username, action, item_id[:256], detail[:512]),
        )


def verify_current_password(username, password):
    with db() as con:
        row = con.execute("SELECT password_hash FROM users WHERE username=?", (username,)).fetchone()
    if not row or not valid_password(password, row["password_hash"]):
        raise HTTPException(401, "Current administrator password is required")


def controller_settings():
    with db() as con:
        row = con.execute("SELECT value FROM controller_settings WHERE key='timezone'").fetchone()
    return {"timezone": row["value"] if row else DEFAULT_DISPLAY_TIMEZONE}


def secure_transport(request):
    """Compatibility hook for deployments that intentionally allow HTTP enrollment."""
    return None


def public_key_fingerprint(public_key):
    try:
        encoded = public_key.strip().split()[1]
        digest_value = hashlib.sha256(base64.b64decode(encoded.encode())).digest()
    except (IndexError, ValueError, binascii.Error) as error:
        raise HTTPException(422, "Invalid OpenSSH public key") from error
    return "SHA256:" + base64.b64encode(digest_value).decode().rstrip("=")


def key_algorithm(private_key):
    if isinstance(private_key, ed25519.Ed25519PrivateKey):
        return "ed25519"
    if isinstance(private_key, rsa.RSAPrivateKey):
        if private_key.key_size < 3072:
            raise HTTPException(422, "Imported RSA keys must be at least 3072 bits")
        return "rsa"
    if isinstance(private_key, ec.EllipticCurvePrivateKey):
        return "ecdsa"
    raise HTTPException(422, "Only Ed25519, ECDSA, and RSA SSH private keys are supported")


def serialize_private_key(private_key):
    return private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH,
        serialization.NoEncryption(),
    ).decode()


def key_material(private_key):
    algorithm = key_algorithm(private_key)
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.OpenSSH,
        serialization.PublicFormat.OpenSSH,
    ).decode()
    return serialize_private_key(private_key), public_key, public_key_fingerprint(public_key), algorithm


def parse_imported_private_key(value, passphrase=None):
    try:
        return serialization.load_ssh_private_key(value.encode(), passphrase.encode() if passphrase else None)
    except (TypeError, ValueError) as error:
        raise HTTPException(422, "Import a valid OpenSSH private key and matching passphrase") from error


def normalize_ssh_host_key(value):
    if not value or not value.strip():
        return ""
    parts = value.strip().split()
    if len(parts) >= 3 and not parts[0].startswith(("ssh-", "ecdsa-")):
        parts = parts[1:]
    if len(parts) < 2:
        raise HTTPException(422, "Provide an OpenSSH host public key")
    normalized = " ".join(parts[:2])
    try:
        serialization.load_ssh_public_key(normalized.encode())
    except ValueError as error:
        raise HTTPException(422, "Provide a valid OpenSSH host public key") from error
    return normalized


def host_key_validation_enabled(node):
    """Keep existing legacy inventory pinned while allowing newly enrolled hosts to opt out."""
    try:
        legacy_trust_disabled = bool(node["legacy_known_hosts_disabled"])
    except (IndexError, KeyError):
        legacy_trust_disabled = False
    return bool(node["ssh_host_key"]) or (node["ssh_auth_state"] == "legacy" and not legacy_trust_disabled)


def ssh_host_key_args(node, known_hosts):
    if host_key_validation_enabled(node):
        return ["-o", f"UserKnownHostsFile={known_hosts}", "-o", "StrictHostKeyChecking=yes"]
    return ["-o", "UserKnownHostsFile=/dev/null", "-o", "StrictHostKeyChecking=no", "-o", "LogLevel=ERROR"]


def enrollment_hostname(log):
    match = re.search(r"ECP_HOSTNAME=([A-Za-z0-9][A-Za-z0-9._-]{0,127})", log)
    return match.group(1) if match else ""


def unique_node_name(con, requested, node_id):
    candidate = requested[:128]
    if not NODE_NAME_RE.fullmatch(candidate):
        return ""
    existing = con.execute("SELECT 1 FROM nodes WHERE name=? AND id<>?", (candidate, node_id)).fetchone()
    if not existing:
        return candidate
    suffix = f"-{node_id}"
    return candidate[:128 - len(suffix)] + suffix


def key_metadata(row):
    if not row:
        return None
    return {
        "key_id": row["key_id"], "algorithm": row["algorithm"], "public_key": row["public_key"],
        "source": row["source"], "state": row["state"], "created_at": row["created_at"],
    }


def legacy_key_metadata():
    try:
        private_key = serialization.load_ssh_private_key(Path(SSH_KEY).read_bytes(), password=None)
        _, public_key, key_id, algorithm = key_material(private_key)
        return {"key_id": key_id, "algorithm": algorithm, "public_key": public_key, "source": "legacy_mounted", "state": "legacy", "created_at": None}
    except (FileNotFoundError, PermissionError, TypeError, ValueError):
        return {"key_id": "", "algorithm": "unknown", "public_key": "", "source": "legacy_mounted", "state": "legacy", "created_at": None}


def controller_key_rows():
    with db() as con:
        rows = con.execute("SELECT * FROM controller_ssh_keys WHERE state IN ('active','candidate') ORDER BY id DESC").fetchall()
    active = next((row for row in rows if row["state"] == "active"), None)
    candidate = next((row for row in rows if row["state"] == "candidate"), None)
    return active, candidate


def managed_key_path(row):
    SSH_RUNTIME.mkdir(parents=True, exist_ok=True)
    safe_key_id = re.sub(r"[^A-Za-z0-9._-]", "_", row["key_id"])
    path = SSH_RUNTIME / f"{safe_key_id}.key"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(open_secret(row["private_key_encrypted"]), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    os.chmod(path, 0o600)
    return str(path)


def remove_managed_key_path(key_id):
    safe_key_id = re.sub(r"[^A-Za-z0-9._-]", "_", key_id)
    (SSH_RUNTIME / f"{safe_key_id}.key").unlink(missing_ok=True)


def active_ssh_key_path():
    active, _ = controller_key_rows()
    return managed_key_path(active) if active else SSH_KEY


def enrollment_key_row():
    active, candidate = controller_key_rows()
    return candidate or active


def controller_key_status():
    active, candidate = controller_key_rows()
    return {
        "active": key_metadata(active) if active else legacy_key_metadata(),
        "candidate": key_metadata(candidate),
        "managed": bool(active),
    }


def stage_controller_key(private_key, source):
    private_value, public_key, key_id, algorithm = key_material(private_key)
    with db() as con:
        enabled_hosts = con.execute("SELECT COUNT(*) AS count FROM nodes WHERE enabled=1").fetchone()["count"]
        retired_candidates = [row["key_id"] for row in con.execute("SELECT key_id FROM controller_ssh_keys WHERE state='candidate'")]
        if retired_candidates:
            installed = con.execute(
                "SELECT name FROM nodes WHERE candidate_key_id IN (" + ",".join("?" * len(retired_candidates)) + ") ORDER BY name",
                retired_candidates,
            ).fetchall()
            if installed:
                raise HTTPException(409, "The current candidate is installed on hosts; activate it or revoke it before staging another key: " + ", ".join(row["name"] for row in installed))
        con.execute("UPDATE controller_ssh_keys SET state='retired' WHERE state='candidate'")
        state = "candidate" if enabled_hosts else "active"
        if state == "active":
            con.execute("UPDATE controller_ssh_keys SET state='retired' WHERE state='active'")
        try:
            cursor = con.execute(
                "INSERT INTO controller_ssh_keys(key_id,algorithm,public_key,private_key_encrypted,source,state) VALUES (?,?,?,?,?,?)",
                (key_id, algorithm, public_key, seal_secret(private_value), source, state),
            )
        except sqlite3.IntegrityError as error:
            raise HTTPException(409, "That SSH key is already registered with the controller") from error
        row = con.execute("SELECT * FROM controller_ssh_keys WHERE id=?", (cursor.lastrowid,)).fetchone()
    for key_id_to_remove in retired_candidates:
        remove_managed_key_path(key_id_to_remove)
    return key_metadata(row)


def candidate_activation_status():
    active, candidate = controller_key_rows()
    if not candidate:
        raise HTTPException(409, "No controller SSH key is staged for activation")
    with db() as con:
        missing = con.execute(
            "SELECT name FROM nodes WHERE enabled=1 AND candidate_key_id<>? ORDER BY name",
            (candidate["key_id"],),
        ).fetchall()
    if missing:
        raise HTTPException(409, "Install and verify the candidate key on every enabled host: " + ", ".join(row["name"] for row in missing))
    return active, candidate


def known_hosts_path(node_ids=None, include_legacy=True):
    RUNTIME.mkdir(parents=True, exist_ok=True)
    lines = []
    if include_legacy:
        try:
            if Path(SSH_KNOWN_HOSTS).exists():
                lines.extend(line for line in Path(SSH_KNOWN_HOSTS).read_text(encoding="utf-8").splitlines() if line.strip())
        except (OSError, UnicodeDecodeError):
            pass
    with db() as con:
        if node_ids:
            rows = con.execute(
                "SELECT address,ssh_port,ssh_host_key FROM nodes WHERE ssh_host_key<>'' AND id IN (" + ",".join("?" * len(node_ids)) + ") ORDER BY id",
                node_ids,
            ).fetchall()
        else:
            rows = con.execute("SELECT address,ssh_port,ssh_host_key FROM nodes WHERE ssh_host_key<>'' ORDER BY id").fetchall()
    for row in rows:
        address = row["address"]
        if any(char.isspace() for char in address):
            continue
        host = address if row["ssh_port"] == 22 else f"[{address}]:{row['ssh_port']}"
        lines.append(f"{host} {row['ssh_host_key']}")
    if not lines and include_legacy and not node_ids:
        return SSH_KNOWN_HOSTS
    suffix = "all" if not node_ids else "-".join(str(node_id) for node_id in sorted(node_ids))
    path = RUNTIME / f"managed_nodes_known_hosts-{suffix}"
    temporary = path.with_suffix(".tmp")
    temporary.write_text("\n".join(dict.fromkeys(lines)) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    os.chmod(path, 0o600)
    return str(path)


def redacted_config(value):
    hidden = {"password", "token", "secret", "api_key", "apikey", "key", "credential"}
    result = {}
    for name, item in value.items():
        if any(part in name.lower() for part in hidden):
            result[name] = "configured" if item else ""
        elif isinstance(item, dict):
            result[name] = redacted_config(item)
        else:
            result[name] = item
    return result


def token_piece(value):
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def read_token_piece(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def signed_token(username):
    issued = str(int(time.time()))
    payload = (username + ":" + issued).encode()
    sig = hmac.new(KEY.encode(), payload, hashlib.sha256).digest()
    return token_piece(payload) + "." + token_piece(sig)


def token_user(token):
    try:
        payload_text, signature_text = token.split(".", 1)
        payload = read_token_piece(payload_text)
        sig = read_token_piece(signature_text)
        expected = hmac.new(KEY.encode(), payload, hashlib.sha256).digest()
        name, issued = payload.decode().rsplit(":", 1)
        if hmac.compare_digest(sig, expected) and int(issued) + 28800 >= time.time():
            return name
    except (ValueError, UnicodeDecodeError, binascii.Error):
        pass
    return None


def run_events_token(run_id):
    issued = str(int(time.time()))
    payload = ("run-events:" + str(run_id) + ":" + issued).encode()
    sig = hmac.new(KEY.encode(), payload, hashlib.sha256).digest()
    return token_piece(payload) + "." + token_piece(sig)


def valid_run_events_token(token, run_id):
    try:
        payload_text, signature_text = token.split(".", 1)
        payload = read_token_piece(payload_text)
        sig = read_token_piece(signature_text)
        expected = hmac.new(KEY.encode(), payload, hashlib.sha256).digest()
        prefix, signed_run_id, issued = payload.decode().split(":", 2)
        return hmac.compare_digest(sig, expected) and prefix == "run-events" and int(signed_run_id) == run_id and int(issued) + 600 >= time.time()
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return False


def slugify(value):
    return value.lower().replace(".", "-").replace("_", "-")


def next_theme_color(con):
    used = {row["theme_color"].upper() for row in con.execute("SELECT theme_color FROM clusters WHERE theme_color IS NOT NULL")}
    return next((color for color in THEME_PALETTE if color not in used), THEME_PALETTE[len(used) % len(THEME_PALETTE)])


def valid_ipv4(value):
    try:
        return isinstance(ipaddress.ip_address(value), ipaddress.IPv4Address)
    except (ValueError, TypeError):
        return False


def version_key(value):
    match = VERSION_RE.fullmatch(str(value or ""))
    return tuple(map(int, match.groups())) if match else None


def image_version(image):
    value = str(image or "")
    tag = value.rsplit(":", 1)[-1] if ":" in value.rsplit("/", 1)[-1] else ""
    return tag if version_key(tag) else ""


def image_for_role(role, version):
    return f"docker.elastic.co/{ROLE_IMAGES[role]}:{version}"


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


def registry_json(url, headers=None):
    request = urllib.request.Request(url, headers=headers or {"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=REGISTRY_REQUEST_TIMEOUT) as response:
            return json.loads(response.read().decode()), response.headers
    except urllib.error.HTTPError as error:
        if error.code != 401:
            raise
        challenge = error.headers.get("WWW-Authenticate", "")
        if not challenge.lower().startswith("bearer "):
            raise
        parts = dict(re.findall(r'([a-zA-Z_]+)="([^"]+)"', challenge))
        realm = parts.get("realm")
        if not realm:
            raise
        query = urllib.parse.urlencode({key: value for key, value in parts.items() if key in {"service", "scope"}})
        token_payload, _ = registry_json(realm + ("?" + query if query else ""))
        token = token_payload.get("token") or token_payload.get("access_token")
        if not token:
            raise HTTPException(503, "Elastic registry did not return an access token")
        with urllib.request.urlopen(urllib.request.Request(url, headers={"Accept": "application/json", "Authorization": "Bearer " + token}), timeout=REGISTRY_REQUEST_TIMEOUT) as response:
            return json.loads(response.read().decode()), response.headers


def registry_tags(repository, cursor):
    cache_key = (repository, cursor)
    cached = REGISTRY_CACHE.get(cache_key)
    if cached and cached[0] + REGISTRY_CACHE_SECONDS > time.time():
        return cached[1]
    url = f"https://docker.elastic.co/v2/{repository}/tags/list?n={REGISTRY_TAG_PAGE_SIZE}"
    if cursor:
        url += "&last=" + urllib.parse.quote(cursor, safe="")
    tags = set()
    error = None
    for _ in range(REGISTRY_TAG_PAGE_LIMIT):
        for attempt in range(3):
            try:
                payload, headers = registry_json(url)
                break
            except (OSError, urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError) as caught:
                error = caught
                if attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
        else:
            raise HTTPException(503, "Unable to retrieve Elastic image versions") from error
        tags.update(tag for tag in payload.get("tags", []) if version_key(tag))
        if len(tags) >= REGISTRY_TAG_RESULT_LIMIT:
            break
        link = headers.get("Link", "")
        match = re.search(r'<([^>]+)>;\s*rel="next"', link)
        if not match:
            break
        url = urllib.parse.urljoin("https://docker.elastic.co", match.group(1))
    if not tags:
        raise HTTPException(503, f"Elastic registry returned no stable versions for {repository}")
    REGISTRY_CACHE[cache_key] = (time.time(), tags)
    return tags


def cluster_repositories(assignments, filebeat_enabled=False):
    repositories = {ROLE_IMAGES[assignment["role"]] for assignment in assignments if assignment["role"] in ROLE_IMAGES}
    if any(assignment["role"] in METRICBEAT_ROLES for assignment in assignments):
        repositories.add(METRICBEAT_IMAGE)
    if filebeat_enabled and assignments:
        repositories.add(FILEBEAT_IMAGE)
    return repositories


def available_versions(assignments, filebeat_enabled=False):
    repositories = cluster_repositories(assignments, filebeat_enabled)
    if not repositories:
        return []
    known = [
        version_key((assignment.get("observation") or {}).get("version", "")) or version_key(assignment.get("image_version", "")) or version_key(assignment.get("desired_version", ""))
        for assignment in assignments
    ]
    known = [version for version in known if version]
    minimum = min(known) if known else version_key(DEFAULT_STACK_VERSION)
    cursor = f"{minimum[0]}.{max(minimum[1] - 1, 0)}.999"
    with ThreadPoolExecutor(max_workers=min(4, len(repositories))) as executor:
        repository_tags = executor.map(lambda repository: registry_tags(repository, cursor), sorted(repositories))
        groups = list(repository_tags)
    common = None
    for tags in groups:
        common = tags if common is None else common.intersection(tags)
    return sorted(common or (), key=version_key, reverse=True)


def observation_is_fresh(observation):
    try:
        return observation and time.time() - calendar.timegm(time.strptime(observation["observed_at"], "%Y-%m-%d %H:%M:%S")) <= VERSION_OBSERVATION_MAX_AGE
    except (KeyError, TypeError, ValueError):
        return False


def validate_membership_network(input):
    if not valid_ipv4(input.data_address) or not valid_ipv4(input.user_address):
        raise HTTPException(422, "Data and user addresses must be IPv4 addresses")
    if input.network_mode == "dedicated":
        if input.data_interface == input.user_interface:
            raise HTTPException(422, "Dedicated mode requires different data and user interfaces")
        if input.data_address == input.user_address:
            raise HTTPException(422, "Dedicated mode requires different data and user addresses")
    elif input.data_interface != input.user_interface or input.data_address != input.user_address:
        raise HTTPException(422, "Shared mode requires the same data and user interface and address")


def membership_ready(member):
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


def require_ready_membership(member):
    if not membership_ready(member):
        raise HTTPException(422, "Configure valid dedicated or shared data and user network bindings before applying or reconciling this workload")


def valid_storage_path(value):
    if not value.startswith("/") or value == "/" or ":" in value or any(char.isspace() for char in value):
        return False
    return not any(value == path or value.startswith(path + "/") for path in PATH_BLOCKLIST)


def validate_config(role, config):
    if role not in ROLE_SPECS:
        raise HTTPException(422, "Unsupported role")
    for key in ("cpu", "memory", "storage_path"):
        if not config.get(key):
            raise HTTPException(422, f"{key} is required")
    if not CPU_RE.fullmatch(str(config["cpu"])) or float(config["cpu"]) <= 0:
        raise HTTPException(422, "CPU must be a positive core value")
    if not MEMORY_RE.fullmatch(str(config["memory"])):
        raise HTTPException(422, "Memory must be a positive size such as 4g")
    if not valid_storage_path(str(config["storage_path"])):
        raise HTTPException(422, "Storage path must be a safe absolute non-system path")
    if role in {"master", "hot", "warm", "ml", "ingest", "coordinating"} and memory_mebibytes(str(config["memory"])) < 2048:
        raise HTTPException(422, "Elasticsearch workloads require at least 2g of memory")
    if role == "logstash" and not str(config.get("pipeline", "")).strip():
        raise HTTPException(422, "A Logstash pipeline is required")


def memory_mebibytes(value):
    number = float(value[:-1])
    unit = value[-1].lower()
    return int(number * {"k": 1 / 1024, "m": 1, "g": 1024, "t": 1024 * 1024}[unit])


def cluster_payload(con, row, desired_state="present", batch_assignment_ids=(), config_overrides=None):
    if desired_state != "purge":
        require_ready_membership(row)
    config_overrides = config_overrides or {}
    config = dict(config_overrides.get(row["id"], open_config(row["config_json"])))
    if row["role"] in {"master", "hot", "warm", "ml", "ingest", "coordinating"}:
        config["jvm_heap"] = f"{max(1024, memory_mebibytes(str(config['memory'])) // 2)}m"
    included_ids = sorted({int(value) for value in batch_assignment_ids})
    state_filter = "cluster_assignments.state='active'"
    state_params = []
    if included_ids:
        state_filter += " OR cluster_assignments.id IN (" + ",".join("?" * len(included_ids)) + ")"
        state_params.extend(included_ids)
    master_rows = con.execute(
        "SELECT cluster_assignments.id, cluster_assignments.node_id, cluster_assignments.config_json, "
        "nodes.name AS node_name, nodes.address AS node_address, memberships.network_mode, memberships.data_interface, memberships.data_address, memberships.user_interface, memberships.user_address "
        "FROM cluster_assignments JOIN nodes ON nodes.id=cluster_assignments.node_id "
        "JOIN memberships ON memberships.cluster_id=cluster_assignments.cluster_id AND memberships.node_id=cluster_assignments.node_id "
        "WHERE cluster_assignments.cluster_id=? AND cluster_assignments.role='master' AND (" + state_filter + ") ORDER BY cluster_assignments.id",
        [row["cluster_id"], *state_params],
    ).fetchall()
    bootstrap = master_rows[0] if master_rows else None
    if desired_state != "purge" and row["role"] != "master" and not bootstrap:
        raise HTTPException(422, "Deploy a master before this workload")
    role_ports = stored_role_ports(row["role_ports_json"], json.loads(row["ports_json"]))
    masters = [{
        "assignment_id": master["id"], "node_id": master["node_id"], "node_name": master["node_name"],
        "node_address": master["node_address"], "network_mode": master["network_mode"],
        "data_address": master["data_address"], "user_address": master["user_address"],
        "workload": f"ecp-{row['slug']}-master-{master['node_id']}", "ports": role_ports["master"],
    } for master in master_rows]
    if bootstrap:
        if desired_state != "purge":
            require_ready_membership(bootstrap)
        bootstrap_config = config_overrides.get(bootstrap["id"], open_config(bootstrap["config_json"]))
        bootstrap_data = {**masters[0], "storage_path": bootstrap_config["storage_path"]}
    else:
        bootstrap_data = None
    services = {}
    for service_role in ("kibana", "fleet-server"):
        service = con.execute(
            "SELECT cluster_assignments.id, cluster_assignments.node_id, nodes.name AS node_name, nodes.address AS node_address, memberships.network_mode, memberships.data_interface, memberships.data_address, memberships.user_interface, memberships.user_address "
            "FROM cluster_assignments JOIN nodes ON nodes.id=cluster_assignments.node_id "
            "JOIN memberships ON memberships.cluster_id=cluster_assignments.cluster_id AND memberships.node_id=cluster_assignments.node_id "
            "WHERE cluster_assignments.cluster_id=? AND cluster_assignments.role=? AND (" + state_filter + ") ORDER BY cluster_assignments.id LIMIT 1",
            [row["cluster_id"], service_role, *state_params],
        ).fetchone()
        if service:
            if desired_state != "purge":
                require_ready_membership(service)
            services[service_role] = {
                "assignment_id": service["id"], "node_id": service["node_id"], "node_name": service["node_name"],
                "node_address": service["node_address"], "network_mode": service["network_mode"], "data_address": service["data_address"], "user_address": service["user_address"],
                "workload": f"ecp-{row['slug']}-{service_role}-{service['node_id']}", "ports": role_ports[service_role],
            }
    if desired_state != "purge" and row["role"] == "fleet-server" and "kibana" not in services:
        raise HTTPException(422, "Deploy Kibana before Fleet Server")
    if desired_state != "purge" and row["role"] == "elastic-agent" and "fleet-server" not in services:
        raise HTTPException(422, "Deploy Fleet Server before Elastic Agent")
    return {
        "cluster": {"id": row["cluster_id"], "name": row["cluster_name"], "slug": row["slug"], "ports": json.loads(row["ports_json"]), "role_ports": role_ports},
        "assignment": {"id": row["id"], "role": row["role"], "config": config, "image_version": row["image_version"] or DEFAULT_STACK_VERSION, "ports": role_ports[row["role"]]},
        "membership": {"node_id": row["node_id"], "network_mode": row["network_mode"], "data_interface": row["data_interface"], "data_address": row["data_address"], "user_interface": row["user_interface"], "user_address": row["user_address"]},
        "bootstrap": bootstrap_data,
        "masters": masters,
        "services": services,
        "credentials": open_config(row["secrets_json"]),
        "desired_state": desired_state,
    }


def profile_conflict(con, cluster_id, role_ports):
    rows = con.execute(
        "SELECT cluster_assignments.node_id, cluster_assignments.role, cluster_assignments.cluster_id, clusters.name, clusters.ports_json, clusters.role_ports_json "
        "FROM cluster_assignments JOIN clusters ON clusters.id=cluster_assignments.cluster_id "
        "WHERE cluster_assignments.state IN ('active','applying')"
    ).fetchall()
    for left in rows:
        left_ports = role_ports if left["cluster_id"] == cluster_id else stored_role_ports(left["role_ports_json"], json.loads(left["ports_json"]))
        for right in rows:
            if left["node_id"] != right["node_id"] or right["cluster_id"] == left["cluster_id"]:
                continue
            right_ports = role_ports if right["cluster_id"] == cluster_id else stored_role_ports(right["role_ports_json"], json.loads(right["ports_json"]))
            used_left = set(role_port_values(left_ports, left["role"]))
            used_right = set(role_port_values(right_ports, right["role"]))
            if used_left.intersection(used_right):
                return f"Port profile conflicts with {right['name']} on a shared host"
    return None


def cluster_record(con, cluster_id):
    cluster = con.execute("SELECT * FROM clusters WHERE id=?", (cluster_id,)).fetchone()
    if not cluster:
        raise HTTPException(404, "Cluster not found")
    result = dict(cluster)
    result["ports"] = json.loads(result.pop("ports_json"))
    result["role_ports"] = stored_role_ports(result.pop("role_ports_json", ""), result["ports"])
    result["theme_color"] = (result.get("theme_color") or THEME_PALETTE[0]).upper()
    result["desired_version"] = result.get("desired_version") or DEFAULT_STACK_VERSION
    result["network_defaults"] = json.loads(result.pop("network_defaults_json", "{}") or "{}")
    result["elasticsearch_settings"] = json.loads(result.pop("elasticsearch_settings_json", "{}") or "{}")
    result["log_monitoring"] = log_monitoring_config(result.pop("observability_json", "{}"))
    members = con.execute(
        "SELECT memberships.cluster_id, memberships.node_id, memberships.network_mode, memberships.data_interface, memberships.data_address, memberships.user_interface, memberships.user_address, nodes.name, nodes.address, nodes.enabled "
        "FROM memberships JOIN nodes ON nodes.id=memberships.node_id WHERE memberships.cluster_id=? ORDER BY nodes.name",
        (cluster_id,),
    ).fetchall()
    result["members"] = [{**dict(member), "network_ready": membership_ready(member)} for member in members]
    assignments = con.execute(
        "SELECT cluster_assignments.*, nodes.name AS node_name, workload_observations.image, workload_observations.digest, "
        "workload_observations.version, workload_observations.running, workload_observations.cached, workload_observations.observed_at, workload_observations.error, "
        "workload_observations.filebeat_state, workload_observations.filebeat_observed_at, workload_observations.filebeat_error "
        "FROM cluster_assignments "
        "JOIN nodes ON nodes.id=cluster_assignments.node_id "
        "LEFT JOIN workload_observations ON workload_observations.assignment_id=cluster_assignments.id "
        "WHERE cluster_id=? AND cluster_assignments.state='active' ORDER BY node_name, role",
        (cluster_id,),
    ).fetchall()
    result["assignments"] = [{
        "id": row["id"], "cluster_id": row["cluster_id"], "node_id": row["node_id"], "node_name": row["node_name"],
        "role": row["role"], "state": row["state"], "revision": row["revision"], "image_version": row["image_version"], "config": redacted_config(open_config(row["config_json"])),
        "observation": ({"image": row["image"], "digest": row["digest"], "version": row["version"], "running": bool(row["running"]), "cached": bool(row["cached"]), "observed_at": row["observed_at"], "error": row["error"]} if row["observed_at"] else None),
        "filebeat": ({"state": row["filebeat_state"], "observed_at": row["filebeat_observed_at"], "error": row["filebeat_error"]} if row["filebeat_observed_at"] else {"state": "disabled", "error": ""}),
    } for row in assignments]
    return result


def assignment_record(con, assignment_id):
    row = con.execute(
        "SELECT cluster_assignments.*, clusters.name AS cluster_name, clusters.slug, clusters.ports_json, clusters.role_ports_json, clusters.secrets_json, "
        "nodes.name AS node_name, nodes.address AS node_address, memberships.network_mode, memberships.data_interface, memberships.data_address, memberships.user_interface, memberships.user_address "
        "FROM cluster_assignments JOIN clusters ON clusters.id=cluster_assignments.cluster_id "
        "JOIN nodes ON nodes.id=cluster_assignments.node_id "
        "JOIN memberships ON memberships.cluster_id=cluster_assignments.cluster_id AND memberships.node_id=cluster_assignments.node_id "
        "WHERE cluster_assignments.id=?",
        (assignment_id,),
    ).fetchone()
    if not row:
        raise HTTPException(404, "Assignment not found")
    return row


def conflict_message(con, cluster_id, node_id, role):
    if not ROLE_SPECS[role]["ports"]:
        return None
    rows = con.execute(
        "SELECT cluster_assignments.role, clusters.name, clusters.ports_json, clusters.role_ports_json FROM cluster_assignments "
        "JOIN clusters ON clusters.id=cluster_assignments.cluster_id "
        "WHERE cluster_assignments.node_id=? AND cluster_assignments.cluster_id<>? AND cluster_assignments.state IN ('active','applying')",
        (node_id, cluster_id),
    ).fetchall()
    target = con.execute("SELECT ports_json,role_ports_json FROM clusters WHERE id=?", (cluster_id,)).fetchone()
    target_ports = stored_role_ports(target["role_ports_json"], json.loads(target["ports_json"]))
    used = set(role_port_values(target_ports, role))
    for row in rows:
        other = stored_role_ports(row["role_ports_json"], json.loads(row["ports_json"]))
        if used.intersection(role_port_values(other, row["role"])):
            return f"Port profile conflicts with {row['name']} on this host"
    return None


async def user(request: Request):
    header = request.headers.get("authorization", "")
    name = token_user(header[7:]) if header.startswith("Bearer ") else None
    if not name:
        raise HTTPException(401, "Authentication required")
    return name


def init():
    if not KEY or not PASSWORD:
        raise RuntimeError("APP_SECRET_KEY and ADMIN_PASSWORD are required")
    DATA.mkdir(parents=True, exist_ok=True)
    RUNS.mkdir(parents=True, exist_ok=True)
    INVENTORIES.mkdir(parents=True, exist_ok=True)
    VARIABLES.mkdir(parents=True, exist_ok=True)
    # Older controller releases wrote materialized private keys below the
    # persistent data volume. The encrypted database record is authoritative.
    old_runtime_keys = DATA / "runtime" / "ssh"
    if old_runtime_keys != SSH_RUNTIME and old_runtime_keys.exists():
        for path in old_runtime_keys.glob("*.key"):
            path.unlink(missing_ok=True)
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY, password_hash TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS controller_settings (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS nodes (
          id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, address TEXT NOT NULL,
          ssh_port INTEGER NOT NULL, ssh_user TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
          ssh_host_key TEXT NOT NULL DEFAULT '', ssh_auth_state TEXT NOT NULL DEFAULT 'legacy',
          ssh_key_id TEXT NOT NULL DEFAULT '', candidate_key_id TEXT NOT NULL DEFAULT '',
          legacy_known_hosts_disabled INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS controller_ssh_keys (
          id INTEGER PRIMARY KEY,
          key_id TEXT UNIQUE NOT NULL,
          algorithm TEXT NOT NULL,
          public_key TEXT NOT NULL,
          private_key_encrypted TEXT NOT NULL,
          source TEXT NOT NULL,
          state TEXT NOT NULL,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS assignments (
          node_id INTEGER NOT NULL, role TEXT NOT NULL, config_json TEXT NOT NULL DEFAULT '{}',
          PRIMARY KEY(node_id, role)
        );
        CREATE TABLE IF NOT EXISTS clusters (
          id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, slug TEXT UNIQUE NOT NULL,
          ports_json TEXT NOT NULL, role_ports_json TEXT NOT NULL DEFAULT '{}', secrets_json TEXT NOT NULL DEFAULT '{}',
          observability_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS memberships (
          cluster_id INTEGER NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
          node_id INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
          network_mode TEXT NOT NULL DEFAULT 'dedicated',
          data_interface TEXT,
          data_address TEXT,
          user_interface TEXT,
          user_address TEXT,
          PRIMARY KEY(cluster_id, node_id)
        );
        CREATE TABLE IF NOT EXISTS cluster_assignments (
          id INTEGER PRIMARY KEY,
          cluster_id INTEGER NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
          node_id INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
          role TEXT NOT NULL,
          config_json TEXT NOT NULL,
          image_version TEXT,
          state TEXT NOT NULL DEFAULT 'active',
          revision INTEGER NOT NULL DEFAULT 1,
          operation_run_id INTEGER REFERENCES runs(id) ON DELETE SET NULL,
          UNIQUE(cluster_id, node_id, role)
        );
        CREATE TABLE IF NOT EXISTS workload_change_batches (
          run_id INTEGER PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
          cluster_id INTEGER NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
          plan_encrypted TEXT NOT NULL,
          completed_json TEXT NOT NULL DEFAULT '[]',
          phase TEXT NOT NULL DEFAULT 'applying'
        );
        CREATE TABLE IF NOT EXISTS workload_observations (
          assignment_id INTEGER PRIMARY KEY REFERENCES cluster_assignments(id) ON DELETE CASCADE,
          image TEXT NOT NULL DEFAULT '', digest TEXT NOT NULL DEFAULT '', version TEXT NOT NULL DEFAULT '',
          running INTEGER NOT NULL DEFAULT 0, cached INTEGER NOT NULL DEFAULT 0, observed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          error TEXT NOT NULL DEFAULT '', filebeat_state TEXT NOT NULL DEFAULT 'disabled',
          filebeat_observed_at TEXT, filebeat_error TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS runs (
          id INTEGER PRIMARY KEY, kind TEXT NOT NULL, target TEXT NOT NULL, status TEXT NOT NULL,
          command_json TEXT NOT NULL, log TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, finished_at TEXT,
          context_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS host_runtime_observations (
          node_id INTEGER PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
          initialized INTEGER NOT NULL DEFAULT 0,
          reachable INTEGER NOT NULL DEFAULT 0,
          podman_socket_active INTEGER NOT NULL DEFAULT 0,
          os_name TEXT NOT NULL DEFAULT '',
          podman_version TEXT NOT NULL DEFAULT '',
          observed_at TEXT,
          last_error TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS audit_events (
          id INTEGER PRIMARY KEY,
          username TEXT NOT NULL,
          action TEXT NOT NULL,
          cluster_id INTEGER,
          item_id TEXT NOT NULL DEFAULT '',
          detail TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """)
        columns = {row["name"] for row in con.execute("PRAGMA table_info(runs)")}
        node_columns = {row["name"] for row in con.execute("PRAGMA table_info(nodes)")}
        cluster_columns = {row["name"] for row in con.execute("PRAGMA table_info(clusters)")}
        membership_columns = {row["name"] for row in con.execute("PRAGMA table_info(memberships)")}
        assignment_columns = {row["name"] for row in con.execute("PRAGMA table_info(cluster_assignments)")}
        observation_columns = {row["name"] for row in con.execute("PRAGMA table_info(workload_observations)")}
        runtime_columns = {row["name"] for row in con.execute("PRAGMA table_info(host_runtime_observations)")}
        for column, definition in {
            "ssh_host_key": "TEXT NOT NULL DEFAULT ''",
            "ssh_auth_state": "TEXT NOT NULL DEFAULT 'legacy'",
            "ssh_key_id": "TEXT NOT NULL DEFAULT ''",
            "candidate_key_id": "TEXT NOT NULL DEFAULT ''",
            "legacy_known_hosts_disabled": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            if column not in node_columns:
                con.execute(f"ALTER TABLE nodes ADD COLUMN {column} {definition}")
        if "role_ports_json" not in cluster_columns:
            con.execute("ALTER TABLE clusters ADD COLUMN role_ports_json TEXT NOT NULL DEFAULT '{}'")
        if "os_name" not in runtime_columns:
            con.execute("ALTER TABLE host_runtime_observations ADD COLUMN os_name TEXT NOT NULL DEFAULT ''")
        if "secrets_json" not in cluster_columns:
            con.execute("ALTER TABLE clusters ADD COLUMN secrets_json TEXT NOT NULL DEFAULT '{}'")
        cluster_additions = {
            "theme_color": "TEXT",
            "desired_version": "TEXT",
            "network_defaults_json": "TEXT NOT NULL DEFAULT '{}'",
            "elasticsearch_settings_json": "TEXT NOT NULL DEFAULT '{}'",
            "observability_json": "TEXT NOT NULL DEFAULT '{}'",
        }
        for column, definition in cluster_additions.items():
            if column not in cluster_columns:
                con.execute(f"ALTER TABLE clusters ADD COLUMN {column} {definition}")
        used_colors = [row["theme_color"] for row in con.execute("SELECT theme_color FROM clusters WHERE theme_color IS NOT NULL")]
        for row in con.execute("SELECT id FROM clusters WHERE theme_color IS NULL ORDER BY id").fetchall():
            color = next((item for item in THEME_PALETTE if item not in used_colors), THEME_PALETTE[row["id"] % len(THEME_PALETTE)])
            con.execute("UPDATE clusters SET theme_color=?,desired_version=COALESCE(desired_version,?),network_defaults_json=CASE WHEN network_defaults_json='{}' THEN ? ELSE network_defaults_json END,elasticsearch_settings_json=CASE WHEN elasticsearch_settings_json='{}' THEN ? ELSE elasticsearch_settings_json END WHERE id=?", (
                color, DEFAULT_STACK_VERSION, NetworkDefaults().model_dump_json(), ElasticsearchSettings().model_dump_json(), row["id"],
            ))
            used_colors.append(color)
        if "context_json" not in columns:
            con.execute("ALTER TABLE runs ADD COLUMN context_json TEXT NOT NULL DEFAULT '{}' ")
        if "image_version" not in assignment_columns:
            con.execute("ALTER TABLE cluster_assignments ADD COLUMN image_version TEXT")
        if "revision" not in assignment_columns:
            con.execute("ALTER TABLE cluster_assignments ADD COLUMN revision INTEGER NOT NULL DEFAULT 1")
        if "operation_run_id" not in assignment_columns:
            con.execute("ALTER TABLE cluster_assignments ADD COLUMN operation_run_id INTEGER")
        if "network_mode" not in membership_columns:
            con.execute("ALTER TABLE memberships ADD COLUMN network_mode TEXT NOT NULL DEFAULT 'dedicated'")
        if "cached" not in observation_columns:
            con.execute("ALTER TABLE workload_observations ADD COLUMN cached INTEGER NOT NULL DEFAULT 0")
        for column, definition in {
            "filebeat_state": "TEXT NOT NULL DEFAULT 'disabled'",
            "filebeat_observed_at": "TEXT",
            "filebeat_error": "TEXT NOT NULL DEFAULT ''",
        }.items():
            if column not in observation_columns:
                con.execute(f"ALTER TABLE workload_observations ADD COLUMN {column} {definition}")
        for column in ("data_interface", "data_address", "user_interface", "user_address"):
            if column not in membership_columns:
                con.execute(f"ALTER TABLE memberships ADD COLUMN {column} TEXT")
        if "advertised_address" in membership_columns:
            con.execute("UPDATE memberships SET user_address=COALESCE(user_address, advertised_address)")
        for row in con.execute("SELECT id,ports_json,role_ports_json FROM clusters").fetchall():
            legacy_ports = json.loads(row["ports_json"])
            role_ports = stored_role_ports(row["role_ports_json"], legacy_ports)
            if row["role_ports_json"] != json.dumps(role_ports, sort_keys=True):
                con.execute("UPDATE clusters SET role_ports_json=? WHERE id=?", (json.dumps(role_ports, sort_keys=True), row["id"]))
        for row in con.execute("SELECT id,secrets_json FROM clusters").fetchall():
            cluster_secrets = open_config(row["secrets_json"])
            if not cluster_secrets.get("monitoring_password"):
                cluster_secrets["monitoring_password"] = secrets.token_hex(24)
                con.execute("UPDATE clusters SET secrets_json=? WHERE id=?", (seal_config(json.dumps(cluster_secrets)), row["id"]))
            if not cluster_secrets.get("filebeat_password"):
                cluster_secrets["filebeat_password"] = secrets.token_hex(24)
                con.execute("UPDATE clusters SET secrets_json=? WHERE id=?", (seal_config(json.dumps(cluster_secrets)), row["id"]))
        for row in con.execute("SELECT id,observability_json FROM clusters").fetchall():
            observability = log_monitoring_config(row["observability_json"])
            if row["observability_json"] != json.dumps(observability, sort_keys=True):
                con.execute("UPDATE clusters SET observability_json=? WHERE id=?", (json.dumps(observability, sort_keys=True), row["id"]))
        con.execute("DELETE FROM assignments")
        con.execute("DELETE FROM cluster_assignments WHERE cluster_id NOT IN (SELECT id FROM clusters) OR node_id NOT IN (SELECT id FROM nodes)")
        con.execute("DELETE FROM memberships WHERE cluster_id NOT IN (SELECT id FROM clusters) OR node_id NOT IN (SELECT id FROM nodes)")
        con.execute(
            "UPDATE workload_change_batches SET phase='rolling_back' WHERE run_id IN (SELECT id FROM runs WHERE status IN ('queued','running'))"
        )
        con.execute(
            "UPDATE runs SET status='recovery_required', finished_at=CURRENT_TIMESTAMP, log=log || ? "
            "WHERE id IN (SELECT run_id FROM workload_change_batches) AND status IN ('queued','running')",
            ("Controller restarted before this workload batch completed; rollback is required.\n",),
        )
        con.execute(
            "UPDATE runs SET status='failed', finished_at=CURRENT_TIMESTAMP, log=log || ? WHERE status IN ('queued','running')",
            ("Controller restarted before this run completed.\n",),
        )
        if not con.execute("SELECT 1 FROM users WHERE username=?", (ADMIN,)).fetchone():
            con.execute("INSERT INTO users VALUES (?, ?)", (ADMIN, digest(PASSWORD)))
        con.execute(
            "INSERT OR IGNORE INTO controller_settings(key,value) VALUES ('timezone',?)",
            (DEFAULT_DISPLAY_TIMEZONE,),
        )
    for directory in (INVENTORIES, VARIABLES):
        for path in directory.glob("run-*.yaml"):
            path.unlink(missing_ok=True)


def inventory(run_id, private_key=None, node_ids=None, password_bootstrap=False, pinned_host_key_only=False):
    with db() as con:
        if node_ids:
            rows = con.execute(
                "SELECT * FROM nodes WHERE id IN (" + ",".join("?" * len(node_ids)) + ") ORDER BY name",
                node_ids,
            ).fetchall()
        else:
            rows = con.execute("SELECT * FROM nodes WHERE enabled=1 ORDER BY name").fetchall()
    key_path = None if password_bootstrap else (private_key or active_ssh_key_path())
    known_hosts = known_hosts_path(node_ids, include_legacy=not pinned_host_key_only)
    variables = {}
    if key_path:
        variables["ansible_ssh_private_key_file"] = key_path
    content = {"all": {"vars": variables, "hosts": {}}}
    for row in rows:
        ssh_args = ssh_host_key_args(row, known_hosts)
        if password_bootstrap:
            ssh_args += [
                "-o", "ControlMaster=no", "-o", "ControlPath=none", "-o", "ControlPersist=no",
                "-o", "PubkeyAuthentication=no", "-o", "PreferredAuthentications=password,keyboard-interactive",
            ]
        content["all"]["hosts"][row["name"]] = {
            "ansible_host": row["address"], "ansible_port": row["ssh_port"], "ansible_user": row["ssh_user"],
            "ansible_ssh_common_args": " ".join(shlex.quote(argument) for argument in ssh_args),
        }
    path = INVENTORIES / f"run-{run_id}.yaml"
    path.write_text(yaml.safe_dump(content, sort_keys=True), encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def password_test_known_hosts(node):
    """Create a temporary host-key pin only when the operator supplied one."""
    token = secrets.token_hex(16)
    if not node["ssh_host_key"]:
        return None
    RUNTIME.mkdir(parents=True, exist_ok=True)
    known_hosts = RUNTIME / f"password-test-{token}.known-hosts"
    host = node["address"] if node["ssh_port"] == 22 else f"[{node['address']}]:{node['ssh_port']}"
    known_hosts.write_text(f"{host} {node['ssh_host_key']}\n", encoding="utf-8")
    os.chmod(known_hosts, 0o600)
    return known_hosts


def password_test_command(node, password_fd, known_hosts):
    ssh_args = ssh_host_key_args(node, str(known_hosts) if known_hosts else "/dev/null")
    return [
        "sshpass", "-d", str(password_fd), "ssh", *ssh_args,
        "-o", "ControlMaster=no", "-o", "ControlPath=none", "-o", "ControlPersist=no",
        "-o", "PubkeyAuthentication=no", "-o", "PasswordAuthentication=yes",
        "-o", "PreferredAuthentications=password,keyboard-interactive",
        "-o", "NumberOfPasswordPrompts=1", "-o", "ConnectTimeout=8",
        "-p", str(node["ssh_port"]), f"{node['ssh_user']}@{node['address']}", "/usr/bin/true",
    ]


def verify_ssh_password(node, password):
    """Verify one password from an inherited pipe without persisting or logging it."""
    known_hosts = None
    password_read = password_write = None
    try:
        known_hosts = password_test_known_hosts(node)
        password_read, password_write = os.pipe()
        process = subprocess.Popen(
            password_test_command(node, password_read, known_hosts),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            pass_fds=(password_read,),
        )
        os.close(password_read)
        password_read = None
        try:
            os.write(password_write, (password + "\n").encode())
        except BrokenPipeError:
            pass
        os.close(password_write)
        password_write = None
        return process.wait() == 0, "Password authentication succeeded."
    except OSError:
        return False, "Password authentication could not be started by the controller."
    finally:
        for descriptor in (password_read, password_write):
            if descriptor is not None:
                os.close(descriptor)
        if known_hosts:
            Path(known_hosts).unlink(missing_ok=True)


def test_ssh_password(node, password):
    authenticated, message = verify_ssh_password(node, password)
    if authenticated:
        return True, message
    if message != "Password authentication could not be started by the controller.":
        return False, "Password authentication failed. Check the SSH user, password, host key, and host reachability."
    return False, message


def add_log(run_id, value):
    with db() as con:
        con.execute("UPDATE runs SET log=log || ? WHERE id=?", (value, run_id))


def stream_command(command, on_line):
    """Run a command with blocking stdio and forward complete output lines."""
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    if process.stdout:
        try:
            for line in process.stdout:
                on_line(line)
        finally:
            process.stdout.close()
    return process.wait()


async def run(run_id, command, temporary_paths=()):
    add_log(run_id, "$ " + " ".join(command) + "\n")
    try:
        returncode = await asyncio.to_thread(stream_command, command, lambda line: add_log(run_id, line))
        status = "succeeded" if returncode == 0 else "failed"
    except Exception as error:
        add_log(run_id, "Runner error: " + str(error) + "\n")
        status = "failed"
    finally:
        for path in temporary_paths:
            Path(path).unlink(missing_ok=True)
    filebeat_cluster_id = None
    with db() as con:
        row = con.execute("SELECT context_json,log FROM runs WHERE id=?", (run_id,)).fetchone()
        context = json.loads(row["context_json"])
        if status == "succeeded" and context.get("purge_assignment_id"):
            con.execute("DELETE FROM cluster_assignments WHERE id=?", (context["purge_assignment_id"],))
        if status == "failed" and context.get("rollback_assignment_id"):
            con.execute(
                "UPDATE cluster_assignments SET config_json=? WHERE id=?",
                (seal_config(json.dumps(context["previous_config"])), context["rollback_assignment_id"]),
            )
            con.execute("UPDATE runs SET log=log || ? WHERE id=?", ("Controller configuration restored after failed resource reconciliation\n", run_id))
        if status == "succeeded" and context.get("enrollment_node_id"):
            node_id = context["enrollment_node_id"]
            key_id = context.get("enrollment_key_id", "")
            if context.get("enrollment_install_key") and key_id:
                key = con.execute("SELECT state FROM controller_ssh_keys WHERE key_id=?", (key_id,)).fetchone()
                if key and key["state"] == "candidate":
                    con.execute(
                        "UPDATE nodes SET candidate_key_id=?,ssh_auth_state='candidate_ready',enabled=? WHERE id=?",
                        (key_id, int(bool(context.get("enrollment_enabled"))), node_id),
                    )
                    con.execute(
                        "INSERT INTO audit_events(username,action,item_id,detail) VALUES (?,?,?,?)",
                        (context.get("enrollment_username", "system"), "controller_ssh_candidate_installed", str(node_id), key_id),
                    )
                else:
                    con.execute(
                        "UPDATE nodes SET ssh_key_id=?,candidate_key_id='',ssh_auth_state='controller_key',enabled=? WHERE id=?",
                        (key_id, int(bool(context.get("enrollment_enabled"))), node_id),
                    )
                    con.execute(
                        "INSERT INTO audit_events(username,action,item_id,detail) VALUES (?,?,?,?)",
                        (context.get("enrollment_username", "system"), "controller_ssh_key_installed", str(node_id), key_id),
                    )
            elif context.get("enrollment_existing_key"):
                con.execute(
                    "UPDATE nodes SET ssh_auth_state='legacy',enabled=? WHERE id=?",
                    (int(bool(context.get("enrollment_enabled"))), node_id),
                )
            else:
                con.execute("UPDATE nodes SET ssh_auth_state='pending',enabled=0 WHERE id=?", (node_id,))
            if context.get("enrollment_auto_name"):
                discovered_name = unique_node_name(con, enrollment_hostname(row["log"]), node_id)
                if discovered_name:
                    con.execute("UPDATE nodes SET name=? WHERE id=?", (discovered_name, node_id))
                    con.execute("UPDATE runs SET target=? WHERE id=?", (discovered_name, run_id))
                    con.execute(
                        "INSERT INTO audit_events(username,action,item_id,detail) VALUES (?,?,?,?)",
                        (context.get("enrollment_username", "system"), "host_inventory_name_discovered", str(node_id), discovered_name),
                    )
                else:
                    con.execute(
                        "UPDATE runs SET log=log || ? WHERE id=?",
                        ("Remote hostname was unavailable; keeping the temporary inventory name.\n", run_id),
                    )
        if status == "succeeded" and context.get("delete_node_after_revoke"):
            con.execute("DELETE FROM nodes WHERE id=?", (context["delete_node_after_revoke"],))
        if status == "succeeded" and context.get("filebeat_reconcile_cluster_id"):
            filebeat_cluster_id = int(context["filebeat_reconcile_cluster_id"])
        con.execute("UPDATE runs SET status=?, finished_at=CURRENT_TIMESTAMP WHERE id=?", (status, run_id))
    if filebeat_cluster_id:
        try:
            companion_run_id = launch_filebeat_reconcile(filebeat_cluster_id, "system")
            add_log(run_id, f"Scheduled Filebeat reconciliation run #{companion_run_id}.\n")
        except HTTPException as error:
            add_log(run_id, f"Filebeat reconciliation was not scheduled: {error.detail}\n")


def launch(kind, target, factory, variables=None, context=None, inventory_nodes=None, private_key=None, password_bootstrap=False, pinned_host_key_only=False):
    with db() as con:
        cursor = con.execute(
            "INSERT INTO runs(kind,target,status,command_json,context_json) VALUES (?,?,'queued','[]',?)",
            (kind, target, json.dumps(context or {})),
        )
        run_id = cursor.lastrowid
    inv = inventory(
        run_id, private_key=private_key, node_ids=inventory_nodes, password_bootstrap=password_bootstrap,
        pinned_host_key_only=pinned_host_key_only,
    )
    variables_path = None
    if variables is not None:
        variables_path = VARIABLES / f"run-{run_id}.yaml"
        variables_path.write_text(yaml.safe_dump(variables, sort_keys=True), encoding="utf-8")
        os.chmod(variables_path, 0o600)
    command = factory(inv, variables_path)
    with db() as con:
        con.execute("UPDATE runs SET status='running', command_json=? WHERE id=?", (json.dumps(command), run_id))
    temporary_paths = [inv]
    if variables_path:
        temporary_paths.append(variables_path)
    asyncio.create_task(run(run_id, command, temporary_paths))
    return run_id


async def run_commands(run_id, commands, result_handler=None, temporary_paths=()):
    succeeded = True
    try:
        for command, metadata in commands:
            add_log(run_id, "$ " + " ".join(command) + "\n")
            output = ""
            try:
                output_lines = []

                def record_line(value):
                    output_lines.append(value)
                    add_log(run_id, value)

                status = await asyncio.to_thread(stream_command, command, record_line)
                output = "".join(output_lines)
            except Exception as error:
                output = "Runner error: " + str(error) + "\n"
                add_log(run_id, output)
                status = 1
            if result_handler:
                result_handler(metadata, output, status == 0)
            if status:
                succeeded = False
                break
    finally:
        for path in temporary_paths:
            Path(path).unlink(missing_ok=True)
    with db() as con:
        con.execute("UPDATE runs SET status=?, finished_at=CURRENT_TIMESTAMP WHERE id=?", ("succeeded" if succeeded else "failed", run_id))


def launch_commands(kind, target, factory, result_handler=None, context=None):
    with db() as con:
        cursor = con.execute(
            "INSERT INTO runs(kind,target,status,command_json,context_json) VALUES (?,?,'running','[]',?)",
            (kind, target, json.dumps(context or {})),
        )
        run_id = cursor.lastrowid
    inv = inventory(run_id)
    commands = factory(inv)
    with db() as con:
        con.execute("UPDATE runs SET command_json=? WHERE id=?", (json.dumps([command for command, _ in commands]), run_id))
    asyncio.create_task(run_commands(run_id, commands, result_handler, [inv]))
    return run_id


def probe_command(inv, cluster, assignment):
    workload = workload_name(cluster, assignment)
    filebeat_workload = workload + "-filebeat"
    expected_image = image_for_role(assignment["role"], assignment.get("image_version") or DEFAULT_STACK_VERSION)
    filebeat_enabled = int(bool(cluster.get("log_monitoring", {}).get("filebeat_enabled")))
    script = (
        f"name={shlex.quote(workload)}; "
        f"filebeat_name={shlex.quote(filebeat_workload)}; "
        f"assignment_id={assignment['id']}; "
        f"expected={shlex.quote(expected_image)}; "
        f"filebeat_enabled={filebeat_enabled}; "
        "if [[ \"$filebeat_enabled\" == 0 ]]; then filebeat_state=disabled; elif podman container exists \"$filebeat_name\" && [[ $(podman inspect --format '{{{{.State.Running}}}}' \"$filebeat_name\") == true ]]; then filebeat_state=running; else filebeat_state=degraded; fi; "
        "if ! podman container exists \"$name\"; then if podman image exists \"$expected\"; then cached=1; else cached=0; fi; printf 'ECP_VERSION=%s|0|%s||%s|%s\\n' \"$assignment_id\" \"$expected\" \"$cached\" \"$filebeat_state\"; exit 0; fi; "
        "podman inspect \"$name\" | python3 -c 'import json,sys; value=json.load(sys.stdin)[0]; "
        "print(\"ECP_VERSION=%s|%s|%s|%s|1|%s\" % (sys.argv[1], \"1\" if value[\"State\"][\"Running\"] else \"0\", "
        "value[\"Config\"][\"Image\"], value[\"Image\"], sys.argv[2]))' \"$assignment_id\" \"$filebeat_state\""
    )
    return ansible(inv, assignment["node_name"], "shell", script)


def record_observation(metadata, output, succeeded):
    match = re.search(r"ECP_VERSION=(\d+)\|([01])\|([^|\r\n]*)\|([^|\r\n]*)(?:\|([01]))?(?:\|([a-z_]+))?", output)
    assignment_id = metadata["assignment_id"]
    if match:
        _, running, image, digest, cached, filebeat_state = match.groups()
        version = image_version(image)
        cached = cached or "0"
        error = "" if succeeded else "Version probe command failed"
    else:
        image = digest = version = ""
        running = cached = "0"
        filebeat_state = None
        error = "Version probe did not return workload details"
    with db() as con:
        if filebeat_state:
            con.execute(
                "INSERT INTO workload_observations(assignment_id,image,digest,version,running,cached,observed_at,error,filebeat_state,filebeat_observed_at,filebeat_error) VALUES (?,?,?,?,?,?,CURRENT_TIMESTAMP,?, ?,CURRENT_TIMESTAMP,?) "
                "ON CONFLICT(assignment_id) DO UPDATE SET image=excluded.image,digest=excluded.digest,version=excluded.version,running=excluded.running,cached=excluded.cached,observed_at=excluded.observed_at,error=excluded.error,filebeat_state=excluded.filebeat_state,filebeat_observed_at=excluded.filebeat_observed_at,filebeat_error=excluded.filebeat_error",
                (assignment_id, image, digest, version, int(running), int(cached), error, filebeat_state, "" if filebeat_state in {"running", "disabled"} else "Filebeat companion is not running"),
            )
        else:
            con.execute(
                "INSERT INTO workload_observations(assignment_id,image,digest,version,running,cached,observed_at,error) VALUES (?,?,?,?,?,?,CURRENT_TIMESTAMP,?) "
                "ON CONFLICT(assignment_id) DO UPDATE SET image=excluded.image,digest=excluded.digest,version=excluded.version,running=excluded.running,cached=excluded.cached,observed_at=excluded.observed_at,error=excluded.error",
                (assignment_id, image, digest, version, int(running), int(cached), error),
            )


def download_command(inv, node_name, image):
    script = (
        f"image={shlex.quote(image)}; "
        "if podman image exists \"$image\"; then echo \"ECP_IMAGE_CACHED=$image\"; "
        "else podman pull \"$image\"; echo \"ECP_IMAGE_PULLED=$image\"; fi; "
        "podman image inspect \"$image\" | python3 -c 'import json,sys; print(json.load(sys.stdin)[0].get(\"Digest\", \"\"))'"
    )
    return ansible(inv, node_name, "shell", script)


def version_details(con, cluster_id, include_candidates=True):
    cluster = cluster_record(con, cluster_id)
    candidates = []
    registry_error = ""
    if include_candidates:
        try:
            candidates = available_versions(cluster["assignments"], cluster["log_monitoring"]["filebeat_enabled"])
        except HTTPException as error:
            registry_error = error.detail
    return {
        "cluster_id": cluster_id,
        "available_versions": candidates,
        "registry_error": registry_error,
        "assignments": [{
            "assignment_id": assignment["id"], "role": assignment["role"], "node_name": assignment["node_name"],
            "desired_version": assignment["image_version"] or DEFAULT_STACK_VERSION,
            "observation": assignment["observation"],
        } for assignment in cluster["assignments"]],
    }


def validate_version_target(cluster, target_version, candidates=None):
    target = version_key(target_version)
    if not target:
        raise HTTPException(422, "Choose a complete release version such as 8.19.0")
    if target_version not in (candidates if candidates is not None else available_versions(cluster["assignments"], cluster["log_monitoring"]["filebeat_enabled"])):
        raise HTTPException(422, "Choose a version available for every active component in this cluster")
    return target


def upgrade_preflight(cluster, target_version, candidates=None):
    target = validate_version_target(cluster, target_version, candidates)
    if not cluster["assignments"]:
        raise HTTPException(422, "Assign workloads before requesting an upgrade")
    members = {member["node_id"]: member for member in cluster["members"]}
    versions = []
    for assignment in cluster["assignments"]:
        if not membership_ready(members.get(assignment["node_id"])):
            raise HTTPException(422, "Configure valid dedicated or shared data and user network bindings before upgrading this cluster")
        observation = assignment["observation"]
        if not observation_is_fresh(observation) or not observation["running"] or observation["error"]:
            raise HTTPException(422, "Refresh running component versions successfully before upgrading")
        current = version_key(observation["version"])
        if not current:
            raise HTTPException(422, "A managed workload does not report a supported release version")
        if target <= current:
            raise HTTPException(422, "The selected version must be newer than every running component")
        if target[0] > current[0] + 1:
            raise HTTPException(422, "Upgrade one major version at a time")
        versions.append(current)
    es_assignments = [assignment for assignment in cluster["assignments"] if assignment["role"] in TOPOLOGY_ES_ROLES]
    if es_assignments:
        master_count = sum(assignment["role"] == "master" for assignment in es_assignments)
        if master_count < 3:
            raise HTTPException(422, "Safe Elasticsearch rolling upgrade requires three healthy master-eligible workloads")
    return any(target[0] > current[0] for current in versions)


async def execute_logged_command(run_id, command):
    add_log(run_id, "$ " + " ".join(command) + "\n")
    try:
        return await asyncio.to_thread(stream_command, command, lambda line: add_log(run_id, line)) == 0
    except Exception as error:
        add_log(run_id, "Runner error: " + str(error) + "\n")
        return False


def upgrade_preflight_command(inv, variables_path, node_name):
    return [
        "ansible-playbook", "-i", str(inv), str(PLAYBOOKS / "cluster-upgrade-preflight.yml"), "--limit", node_name,
        "--private-key", active_ssh_key_path(), "--extra-vars", "@" + str(variables_path),
    ]


async def run_upgrade(run_id, cluster_id, target_version, inventory_path, assignment_ids):
    paths = [inventory_path]
    succeeded = False
    try:
        with db() as con:
            first = assignment_record(con, assignment_ids[0])
            preflight_payload = cluster_payload(con, first)
            cluster = cluster_record(con, cluster_id)
            observed_versions = [item["observation"]["version"] for item in cluster["assignments"] if item["observation"]]
            preflight_payload["target_version"] = target_version
            preflight_payload["upgrade_major"] = any(version_key(target_version)[0] > version_key(value)[0] for value in observed_versions if version_key(value))
        preflight_vars = VARIABLES / f"run-{run_id}-preflight.yaml"
        preflight_vars.write_text(yaml.safe_dump(preflight_payload, sort_keys=True), encoding="utf-8")
        os.chmod(preflight_vars, 0o600)
        paths.append(preflight_vars)
        if not await execute_logged_command(run_id, upgrade_preflight_command(inventory_path, preflight_vars, first["node_name"])):
            return
        for index, assignment_id in enumerate(assignment_ids):
            with db() as con:
                row = assignment_record(con, assignment_id)
                cluster = cluster_record(con, cluster_id)
                assignment = next(item for item in cluster["assignments"] if item["id"] == assignment_id)
                previous_version = assignment["observation"]["version"]
                payload = cluster_payload(con, row)
                payload["assignment"]["image_version"] = target_version
            variables_path = VARIABLES / f"run-{run_id}-upgrade-{index}.yaml"
            variables_path.write_text(yaml.safe_dump(payload, sort_keys=True), encoding="utf-8")
            os.chmod(variables_path, 0o600)
            paths.append(variables_path)
            if await execute_logged_command(run_id, reconcile_command(inventory_path, variables_path, row["node_name"])):
                with db() as con:
                    con.execute("UPDATE cluster_assignments SET image_version=? WHERE id=?", (target_version, assignment_id))
                continue
            add_log(run_id, f"Upgrade failed for {row['role']} on {row['node_name']}; restoring {previous_version}.\n")
            rollback_payload = dict(payload)
            rollback_payload["assignment"] = dict(payload["assignment"])
            rollback_payload["assignment"]["image_version"] = previous_version
            rollback_path = VARIABLES / f"run-{run_id}-rollback-{index}.yaml"
            rollback_path.write_text(yaml.safe_dump(rollback_payload, sort_keys=True), encoding="utf-8")
            os.chmod(rollback_path, 0o600)
            paths.append(rollback_path)
            await execute_logged_command(run_id, reconcile_command(inventory_path, rollback_path, row["node_name"]))
            return
        succeeded = True
    finally:
        for path in paths:
            Path(path).unlink(missing_ok=True)
        with db() as con:
            con.execute("UPDATE runs SET status=?, finished_at=CURRENT_TIMESTAMP WHERE id=?", ("succeeded" if succeeded else "failed", run_id))
        if succeeded:
            try:
                companion_run_id = launch_filebeat_reconcile(cluster_id, "system")
                add_log(run_id, f"Scheduled Filebeat reconciliation run #{companion_run_id}.\n")
            except HTTPException as error:
                add_log(run_id, f"Filebeat reconciliation was not scheduled: {error.detail}\n")


def launch_upgrade(cluster_id, target_version, candidates=None):
    with db() as con:
        cluster = cluster_record(con, cluster_id)
        if con.execute("SELECT 1 FROM runs WHERE status IN ('queued','running') AND target LIKE ?", (cluster["name"] + ":%",)).fetchone():
            raise HTTPException(409, "Wait for the active cluster operation to finish")
        major_upgrade = upgrade_preflight(cluster, target_version, candidates)
        ordered = sorted(cluster["assignments"], key=lambda item: (UPGRADE_ORDER.index(item["role"]), item["node_name"], item["id"]))
        cursor = con.execute(
            "INSERT INTO runs(kind,target,status,command_json,context_json) VALUES (?,?, 'running','[]',?)",
            ("upgrade", cluster["name"] + ":upgrade:" + target_version, json.dumps({"target_version": target_version, "major_upgrade": major_upgrade})),
        )
        run_id = cursor.lastrowid
    inv = inventory(run_id)
    asyncio.create_task(run_upgrade(run_id, cluster_id, target_version, inv, [assignment["id"] for assignment in ordered]))
    return run_id


def workload_change_sort_key(change):
    return (WORKLOAD_DEPLOY_ORDER.index(change["role"]), change["node_name"], change["assignment_id"])


def active_cluster_operation(con, cluster_name):
    return con.execute(
        "SELECT 1 FROM runs WHERE status IN ('queued','running','recovery_required') AND target LIKE ?",
        (cluster_name + ":%",),
    ).fetchone()


def active_assignments_for_change_set(con, cluster_id):
    return con.execute(
        "SELECT cluster_assignments.*, nodes.name AS node_name, nodes.enabled, memberships.network_mode, memberships.data_interface, memberships.data_address, memberships.user_interface, memberships.user_address "
        "FROM cluster_assignments JOIN nodes ON nodes.id=cluster_assignments.node_id "
        "JOIN memberships ON memberships.cluster_id=cluster_assignments.cluster_id AND memberships.node_id=cluster_assignments.node_id "
        "WHERE cluster_assignments.cluster_id=? AND cluster_assignments.state='active' ORDER BY cluster_assignments.id",
        (cluster_id,),
    ).fetchall()


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


def validate_workload_change_set(con, cluster_id, input):
    cluster = cluster_record(con, cluster_id)
    if active_cluster_operation(con, cluster["name"]):
        raise HTTPException(409, "Wait for the active cluster operation to finish")
    active = [dict(row) for row in active_assignments_for_change_set(con, cluster_id)]
    active_by_id = {row["id"]: row for row in active}
    planned = []
    changed_ids = set()
    for change in input.changes:
        item = change.model_dump()
        if item["kind"] == "create":
            validate_config(item["role"], item["config"])
            member = con.execute(
                "SELECT nodes.name AS node_name,nodes.enabled,memberships.network_mode,memberships.data_interface,memberships.data_address,memberships.user_interface,memberships.user_address "
                "FROM memberships JOIN nodes ON nodes.id=memberships.node_id WHERE memberships.cluster_id=? AND memberships.node_id=?",
                (cluster_id, item["node_id"]),
            ).fetchone()
            if not member:
                raise HTTPException(422, "Add the host to this cluster first")
            if not member["enabled"]:
                raise HTTPException(422, "Enable the host before applying a role")
            require_ready_membership(member)
            conflict = conflict_message(con, cluster_id, item["node_id"], item["role"])
            if conflict:
                raise HTTPException(409, conflict)
            if any(row["node_id"] == item["node_id"] and row["role"] == item["role"] for row in active):
                raise HTTPException(409, "This role is already managed on the selected host")
            planned.append({**item, "node_name": member["node_name"]})
            continue

        row = active_by_id.get(item["assignment_id"])
        if not row:
            raise HTTPException(404, "Managed workload not found")
        if row["operation_run_id"]:
            raise HTTPException(409, "This workload is already part of an active change set")
        if row["revision"] != item["expected_revision"]:
            raise HTTPException(409, "This workload changed since it was staged; refresh and stage it again")
        if row["id"] in changed_ids:
            raise HTTPException(422, "A workload can only appear once in a pending change set")
        changed_ids.add(row["id"])
        if item["kind"] == "resources":
            next_config = {**open_config(row["config_json"]), **item["config"]}
            validate_config(row["role"], next_config)
            if not row["enabled"]:
                raise HTTPException(422, "Enable the host before applying a role")
            require_ready_membership(row)
            planned.append({**item, "node_id": row["node_id"], "node_name": row["node_name"], "role": row["role"], "config": next_config, "previous_config": open_config(row["config_json"])})
        else:
            planned.append({**item, "node_id": row["node_id"], "node_name": row["node_name"], "role": row["role"]})

    final_assignments = [row for row in active if row["id"] not in {item["assignment_id"] for item in planned if item["kind"] == "detach"}]
    final_assignments.extend({"node_id": item["node_id"], "node_name": item["node_name"], "role": item["role"]} for item in planned if item["kind"] == "create")
    validate_final_workload_ports(cluster, final_assignments)
    active_masters = [row for row in active if row["role"] == "master"]
    initial_master = active_masters[0] if active_masters else None
    detached = {item["assignment_id"] for item in planned if item["kind"] == "detach"}
    if initial_master and initial_master["id"] in detached and final_assignments:
        raise HTTPException(409, "Detach the dependent cluster roles before removing the initial master")
    final_roles = {item["role"] for item in final_assignments}
    if any(item["kind"] == "create" and item["role"] != "master" for item in planned) and "master" not in final_roles:
        raise HTTPException(422, "Deploy a master before this workload")
    if "fleet-server" in final_roles and "kibana" not in final_roles:
        raise HTTPException(422, "Deploy Kibana before Fleet Server")
    if "elastic-agent" in final_roles and "fleet-server" not in final_roles:
        raise HTTPException(422, "Deploy Fleet Server before Elastic Agent")
    return cluster, planned


def batch_plan(con, run_id):
    row = con.execute("SELECT plan_encrypted FROM workload_change_batches WHERE run_id=?", (run_id,)).fetchone()
    if not row:
        raise RuntimeError("Workload change batch is unavailable")
    return open_config(row["plan_encrypted"])


def record_batch_progress(con, run_id, completed):
    con.execute(
        "UPDATE workload_change_batches SET completed_json=? WHERE run_id=?",
        (json.dumps([item["client_id"] for item in completed]), run_id),
    )


async def execute_workload_change_reconcile(run_id, inv, payload, name, suffix):
    variables_path = VARIABLES / f"run-{run_id}-workload-{suffix}.yaml"
    variables_path.write_text(yaml.safe_dump(payload, sort_keys=True), encoding="utf-8")
    os.chmod(variables_path, 0o600)
    try:
        return await execute_logged_command(run_id, reconcile_command(inv, variables_path, name))
    finally:
        variables_path.unlink(missing_ok=True)


def workload_change_payload(con, item, plan, desired_state="present"):
    row = assignment_record(con, item["assignment_id"])
    created_ids = [change["assignment_id"] for change in plan["changes"] if change["kind"] == "create"]
    config_overrides = {
        change["assignment_id"]: change["config"]
        for change in plan["changes"] if change["kind"] == "resources"
    }
    if item["kind"] == "resources" and desired_state == "present":
        config_overrides[item["assignment_id"]] = item["config"]
    if item.get("previous_config") and desired_state == "present":
        config_overrides[item["assignment_id"]] = item["previous_config"]
    return cluster_payload(
        con,
        row,
        desired_state,
        batch_assignment_ids=created_ids,
        config_overrides=config_overrides,
    )


async def rollback_workload_change_batch(run_id, inv, plan, completed):
    rolled_back = True
    for index, item in enumerate(reversed(completed)):
        try:
            with db() as con:
                if item["kind"] == "create":
                    payload = workload_change_payload(con, item, plan, "purge")
                else:
                    payload = workload_change_payload(con, {**item, "previous_config": item["previous_config"]}, plan)
            if not await execute_workload_change_reconcile(run_id, inv, payload, item["node_name"], f"rollback-{index}"):
                rolled_back = False
        except Exception as error:
            add_log(run_id, f"Rollback preparation failed for {item['role']} on {item['node_name']}: {error}\n")
            rolled_back = False
    return rolled_back


def release_workload_change_batch(con, run_id, plan):
    for item in plan["changes"]:
        if item["kind"] == "create":
            con.execute("DELETE FROM cluster_assignments WHERE id=? AND operation_run_id=?", (item["assignment_id"], run_id))
        else:
            con.execute("UPDATE cluster_assignments SET operation_run_id=NULL WHERE id=? AND operation_run_id=?", (item["assignment_id"], run_id))
    con.execute("DELETE FROM workload_change_batches WHERE run_id=?", (run_id,))


async def recover_workload_change_batch(run_id):
    inventory_path = None
    try:
        with db() as con:
            plan = batch_plan(con, run_id)
            progress = con.execute("SELECT completed_json FROM workload_change_batches WHERE run_id=?", (run_id,)).fetchone()
            completed_ids = set(json.loads(progress["completed_json"]))
        completed = [item for item in plan["changes"] if item["client_id"] in completed_ids]
        inventory_path = inventory(run_id)
        if not await rollback_workload_change_batch(run_id, inventory_path, plan, completed):
            return
        with db() as con:
            release_workload_change_batch(con, run_id, plan)
            con.execute("UPDATE runs SET status='failed',finished_at=CURRENT_TIMESTAMP,log=log || ? WHERE id=?", ("Interrupted workload batch rolled back after controller restart.\n", run_id))
    except Exception as error:
        add_log(run_id, "Recovery rollback error: " + str(error) + "\n")
    finally:
        if inventory_path:
            inventory_path.unlink(missing_ok=True)


async def recover_workload_change_batches():
    with db() as con:
        run_ids = [row["run_id"] for row in con.execute(
            "SELECT workload_change_batches.run_id FROM workload_change_batches JOIN runs ON runs.id=workload_change_batches.run_id WHERE runs.status='recovery_required'"
        ).fetchall()]
    for run_id in run_ids:
        asyncio.create_task(recover_workload_change_batch(run_id))


async def run_workload_change_batch(run_id, inventory_path):
    succeeded = False
    companion_cluster_id = None
    completed = []
    plan = None
    try:
        with db() as con:
            plan = batch_plan(con, run_id)
        executable = [change for change in plan["changes"] if change["kind"] in {"create", "resources"}]
        executable.sort(key=workload_change_sort_key)
        for index, item in enumerate(executable):
            completed.append(item)
            with db() as con:
                record_batch_progress(con, run_id, completed)
            with db() as con:
                payload = workload_change_payload(con, item, plan)
            if not await execute_workload_change_reconcile(run_id, inventory_path, payload, item["node_name"], str(index)):
                add_log(run_id, f"Batch apply failed for {item['role']} on {item['node_name']}; starting rollback.\n")
                break
        else:
            with db() as con:
                for item in plan["changes"]:
                    if item["kind"] == "create":
                        con.execute("UPDATE cluster_assignments SET state='active',operation_run_id=NULL WHERE id=? AND operation_run_id=?", (item["assignment_id"], run_id))
                    elif item["kind"] == "resources":
                        con.execute("UPDATE cluster_assignments SET config_json=?,revision=revision+1,operation_run_id=NULL WHERE id=? AND operation_run_id=?", (seal_config(json.dumps(item["config"])), item["assignment_id"], run_id))
                    else:
                        con.execute("DELETE FROM cluster_assignments WHERE id=? AND operation_run_id=?", (item["assignment_id"], run_id))
                con.execute("DELETE FROM workload_change_batches WHERE run_id=?", (run_id,))
            succeeded = True
            companion_cluster_id = plan["cluster_id"]
            return

        with db() as con:
            con.execute("UPDATE workload_change_batches SET phase='rolling_back' WHERE run_id=?", (run_id,))
        if not await rollback_workload_change_batch(run_id, inventory_path, plan, completed):
            add_log(run_id, "Rollback requires recovery before workload changes can continue.\n")
            with db() as con:
                con.execute("UPDATE runs SET status='recovery_required',finished_at=CURRENT_TIMESTAMP WHERE id=?", (run_id,))
            return
        with db() as con:
            release_workload_change_batch(con, run_id, plan)
    except Exception as error:
        add_log(run_id, "Batch runner error: " + str(error) + "\n")
        with db() as con:
            con.execute("UPDATE workload_change_batches SET phase='rolling_back' WHERE run_id=?", (run_id,))
            con.execute("UPDATE runs SET status='recovery_required',finished_at=CURRENT_TIMESTAMP WHERE id=?", (run_id,))
        return
    finally:
        inventory_path.unlink(missing_ok=True)
        if succeeded:
            with db() as con:
                con.execute("UPDATE runs SET status='succeeded',finished_at=CURRENT_TIMESTAMP WHERE id=?", (run_id,))
        elif plan:
            with db() as con:
                status = con.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()["status"]
                if status != "recovery_required":
                    con.execute("UPDATE runs SET status='failed',finished_at=CURRENT_TIMESTAMP WHERE id=?", (run_id,))
        if succeeded and companion_cluster_id:
            try:
                companion_run_id = launch_filebeat_reconcile(companion_cluster_id, "system")
                add_log(run_id, f"Scheduled Filebeat reconciliation run #{companion_run_id}.\n")
            except HTTPException as error:
                add_log(run_id, f"Filebeat reconciliation was not scheduled: {error.detail}\n")


def launch_workload_change_batch(cluster_id, input):
    with db() as con:
        cluster, plan_changes = validate_workload_change_set(con, cluster_id, input)
        cursor = con.execute(
            "INSERT INTO runs(kind,target,status,command_json,context_json) VALUES (?,?,'running','[]',?)",
            ("workload-apply", cluster["name"] + ":workload-apply", json.dumps({"change_count": len(plan_changes), "kinds": [item["kind"] for item in plan_changes]})),
        )
        run_id = cursor.lastrowid
        for item in plan_changes:
            if item["kind"] == "create":
                cursor = con.execute(
                    "INSERT INTO cluster_assignments(cluster_id,node_id,role,config_json,state,operation_run_id) VALUES (?,?,?,?, 'applying',?)",
                    (cluster_id, item["node_id"], item["role"], seal_config(json.dumps(item["config"])), run_id),
                )
                item["assignment_id"] = cursor.lastrowid
            else:
                cursor = con.execute(
                    "UPDATE cluster_assignments SET operation_run_id=? WHERE id=? AND state='active' AND revision=? AND operation_run_id IS NULL",
                    (run_id, item["assignment_id"], item["expected_revision"]),
                )
                if not cursor.rowcount:
                    raise HTTPException(409, "This workload changed since it was staged; refresh and stage it again")
        plan = {"cluster_id": cluster_id, "changes": plan_changes}
        con.execute(
            "INSERT INTO workload_change_batches(run_id,cluster_id,plan_encrypted) VALUES (?,?,?)",
            (run_id, cluster_id, seal_config(json.dumps(plan))),
        )
    inv = inventory(run_id)
    asyncio.create_task(run_workload_change_batch(run_id, inv))
    return run_id


def ansible(inv, target, module, args):
    return ["ansible", target, "-i", str(inv), "-m", module, "-a", args, "-o", "--private-key", active_ssh_key_path()]


def reconcile_command(inv, variables_path, name):
    return [
        "ansible-playbook", "-i", str(inv), str(PLAYBOOKS / "cluster-reconcile.yml"), "--limit", name,
        "--private-key", active_ssh_key_path(), "--extra-vars", "@" + str(variables_path),
    ]


def filebeat_reconcile_command(inv, variables_path, name):
    return [
        "ansible-playbook", "-i", str(inv), str(PLAYBOOKS / "filebeat-reconcile.yml"), "--limit", name,
        "--private-key", active_ssh_key_path(), "--extra-vars", "@" + str(variables_path),
    ]


def filebeat_payload(con, row):
    cluster = cluster_record(con, row["cluster_id"])
    payload = cluster_payload(con, row, "purge")
    payload["log_monitoring"] = cluster["log_monitoring"]
    payload["filebeat_image"] = filebeat_image(row["image_version"] or DEFAULT_STACK_VERSION)
    payload["filebeat_username"] = f"elkeeper_filebeat_{row['cluster_id']}"
    payload["filebeat_role"] = f"elkeeper_filebeat_writer_{row['cluster_id']}"
    return payload


async def execute_filebeat_reconcile(run_id, inv, payload, name, suffix):
    variables_path = VARIABLES / f"run-{run_id}-filebeat-{suffix}.yaml"
    variables_path.write_text(yaml.safe_dump(payload, sort_keys=True), encoding="utf-8")
    os.chmod(variables_path, 0o600)
    command = filebeat_reconcile_command(inv, variables_path, name)
    add_log(run_id, "$ " + " ".join(command) + "\n")
    output_lines = []
    try:
        def record_line(value):
            output_lines.append(value)
            add_log(run_id, value)

        succeeded = await asyncio.to_thread(stream_command, command, record_line) == 0
        return succeeded, "".join(output_lines)
    except Exception as error:
        output = "Runner error: " + str(error) + "\n"
        add_log(run_id, output)
        return False, output
    finally:
        variables_path.unlink(missing_ok=True)


def record_filebeat_observation(assignment_id, output, succeeded):
    match = re.search(r"ECP_FILEBEAT=(\d+)\|([a-z_]+)", output)
    if match and int(match.group(1)) == assignment_id:
        state = match.group(2)
        error = "" if succeeded or state == "pending" else "Filebeat reconciliation failed"
    else:
        state = "degraded" if succeeded else "degraded"
        error = "Filebeat reconciliation did not report companion status" if succeeded else "Filebeat reconciliation failed"
    with db() as con:
        con.execute(
            "INSERT INTO workload_observations(assignment_id,filebeat_state,filebeat_observed_at,filebeat_error) VALUES (?,?,CURRENT_TIMESTAMP,?) "
            "ON CONFLICT(assignment_id) DO UPDATE SET filebeat_state=excluded.filebeat_state,filebeat_observed_at=excluded.filebeat_observed_at,filebeat_error=excluded.filebeat_error",
            (assignment_id, state, error),
        )


async def run_filebeat_reconcile(run_id, cluster_id, inventory_path):
    succeeded = True
    try:
        with db() as con:
            cluster = cluster_record(con, cluster_id)
        assignments = cluster["assignments"]
        if not assignments:
            add_log(run_id, "No managed workloads require Filebeat reconciliation.\n")
            return
        for index, assignment in enumerate(assignments):
            with db() as con:
                row = assignment_record(con, assignment["id"])
                payload = filebeat_payload(con, row)
            result, output = await execute_filebeat_reconcile(run_id, inventory_path, payload, assignment["node_name"], str(index))
            record_filebeat_observation(assignment["id"], output, result)
            if not result:
                succeeded = False
    except Exception as error:
        succeeded = False
        add_log(run_id, "Filebeat reconciliation error: " + str(error) + "\n")
    finally:
        inventory_path.unlink(missing_ok=True)
        with db() as con:
            con.execute("UPDATE runs SET status=?,finished_at=CURRENT_TIMESTAMP WHERE id=?", ("succeeded" if succeeded else "failed", run_id))


def launch_filebeat_reconcile(cluster_id, username):
    with db() as con:
        cluster = cluster_record(con, cluster_id)
        if active_cluster_operation(con, cluster["name"]):
            raise HTTPException(409, "Wait for the active cluster operation to finish")
        cursor = con.execute(
            "INSERT INTO runs(kind,target,status,command_json,context_json) VALUES (?,?,'running','[]',?)",
            ("filebeat-reconcile", cluster["name"] + ":filebeat-reconcile", json.dumps({"filebeat_enabled": cluster["log_monitoring"]["filebeat_enabled"]})),
        )
        run_id = cursor.lastrowid
    inventory_path = inventory(run_id)
    asyncio.create_task(run_filebeat_reconcile(run_id, cluster_id, inventory_path))
    audit_event(username, "cluster_filebeat_reconcile", str(cluster_id), "enabled" if cluster["log_monitoring"]["filebeat_enabled"] else "disabled")
    return run_id


@asynccontextmanager
async def life(_):
    init()
    from app import console

    await console.telemetry.start()
    await recover_workload_change_batches()
    try:
        yield
    finally:
        await console.telemetry.stop()


app = FastAPI(title="Elastic Control Plane", lifespan=life)
app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets", check_dir=False), name="assets")


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; connect-src 'self'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.get("/")
async def index():
    return RedirectResponse("/dashboard")


@app.get("/api/health")
async def health():
    return {"status": "ok", "roles": [{"id": key, "label": value["label"]} for key, value in ROLE_SPECS.items()]}


@app.post("/api/auth/login")
async def login(input: Login):
    with db() as con:
        row = con.execute("SELECT password_hash FROM users WHERE username=?", (input.username,)).fetchone()
    if not row or not valid_password(input.password, row["password_hash"]):
        raise HTTPException(401, "Invalid username or password")
    return {"token": signed_token(input.username), "username": input.username}


@app.get("/api/controller/ssh-key")
async def controller_ssh_key(_: Annotated[str, Depends(user)]):
    return controller_key_status()


@app.get("/api/controller/settings")
async def get_controller_settings(_: Annotated[str, Depends(user)]):
    return controller_settings()


@app.put("/api/controller/settings")
async def update_controller_settings(input: ControllerSettingsInput, username: Annotated[str, Depends(user)]):
    with db() as con:
        con.execute(
            "INSERT INTO controller_settings(key,value) VALUES ('timezone',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (input.timezone,),
        )
    audit_event(username, "controller_display_timezone_updated", "timezone", input.timezone)
    return controller_settings()


@app.post("/api/controller/ssh-key/generate")
async def generate_controller_ssh_key(input: ControllerPassword, request: Request, username: Annotated[str, Depends(user)]):
    verify_current_password(username, input.password)
    key = stage_controller_key(ed25519.Ed25519PrivateKey.generate(), "generated")
    audit_event(username, "controller_ssh_key_generated", key["key_id"], key["state"])
    return {"key": key, "status": controller_key_status()}


@app.post("/api/controller/ssh-key/import")
async def import_controller_ssh_key(input: ControllerKeyImport, request: Request, username: Annotated[str, Depends(user)]):
    verify_current_password(username, input.password)
    key = stage_controller_key(parse_imported_private_key(input.private_key, input.passphrase), "imported")
    audit_event(username, "controller_ssh_key_imported", key["key_id"], key["state"])
    return {"key": key, "status": controller_key_status()}


@app.post("/api/controller/ssh-key/activate")
async def activate_controller_ssh_key(input: ControllerPassword, request: Request, username: Annotated[str, Depends(user)]):
    verify_current_password(username, input.password)
    active, candidate = candidate_activation_status()
    with db() as con:
        if active:
            con.execute("UPDATE controller_ssh_keys SET state='retired' WHERE id=?", (active["id"],))
        con.execute("UPDATE controller_ssh_keys SET state='active' WHERE id=?", (candidate["id"],))
        con.execute(
            "UPDATE nodes SET ssh_key_id=?,candidate_key_id='',ssh_auth_state='controller_key' WHERE candidate_key_id=?",
            (candidate["key_id"], candidate["key_id"]),
        )
    if active:
        remove_managed_key_path(active["key_id"])
    audit_event(username, "controller_ssh_key_activated", candidate["key_id"], "candidate activated after host verification")
    return controller_key_status()


def enrollment_variables(node, key, password=None, install_controller_key=True):
    known_hosts = known_hosts_path([node["id"]], include_legacy=False)
    values = {
        "controller_public_key": key["public_key"],
        "controller_key_path": managed_key_path(key),
        "controller_known_hosts_path": known_hosts if host_key_validation_enabled(node) else "/dev/null",
        "controller_ssh_host_key_checking": "yes" if host_key_validation_enabled(node) else "no",
        "controller_ssh_user": node["ssh_user"],
        "controller_address": node["address"],
        "controller_ssh_port": node["ssh_port"],
        "install_controller_key": bool(install_controller_key),
    }
    if password is not None:
        values["ansible_password"] = password
    return values


def enrollment_context(node_id, enabled, key_id="", install_controller_key=False, existing_key=False, auto_name=False, username=""):
    return {
        "enrollment_node_id": node_id,
        "enrollment_enabled": bool(enabled),
        "enrollment_key_id": key_id,
        "enrollment_install_key": bool(install_controller_key),
        "enrollment_existing_key": bool(existing_key),
        "enrollment_auto_name": bool(auto_name),
        "enrollment_username": username,
    }


def launch_password_enrollment(node, password, install_controller_key, username, auto_name=False):
    key = enrollment_key_row()
    if install_controller_key and not key:
        raise HTTPException(409, "Generate or import a controller-owned SSH key before password bootstrap")
    if install_controller_key:
        variables = enrollment_variables(node, key, password, True)
        context = enrollment_context(
            node["id"], node["enabled"], key["key_id"], True,
            auto_name=auto_name, username=username,
        )
    else:
        variables = {"ansible_password": password, "install_controller_key": False}
        context = enrollment_context(node["id"], False, auto_name=auto_name, username=username)
    run_id = launch(
        "host-enroll", node["name"],
        lambda inv, variables_path: [
            "ansible-playbook", "-i", str(inv), str(PLAYBOOKS / "host-bootstrap-key.yml"), "--limit", node["name"],
            "--extra-vars", "@" + str(variables_path),
        ],
        variables=variables,
        context=context,
        inventory_nodes=[node["id"]],
        password_bootstrap=True,
        pinned_host_key_only=True,
    )
    audit_event(username, "host_password_bootstrap", str(node["id"]), "controller key installation requested")
    return run_id


def launch_key_enrollment_probe(node, username, auto_name=False):
    key = enrollment_key_row()
    if not key:
        raise HTTPException(409, "No controller-owned SSH key is configured")
    key_path = managed_key_path(key)
    return launch(
        "host-enroll", node["name"],
        lambda inv, _variables: [
            "ansible", node["name"], "-i", str(inv), "-m", "shell", "-a",
            "hostname -s | sed 's/^/ECP_HOSTNAME=/'", "-o", "--private-key", key_path,
        ],
        context=enrollment_context(node["id"], node["enabled"], key["key_id"], True, True, auto_name, username),
        inventory_nodes=[node["id"]],
        private_key=key_path,
        pinned_host_key_only=True,
    )


@app.post("/api/nodes/test-password")
async def test_node_password(input: NodePasswordTest, username: Annotated[str, Depends(user)]):
    node = {
        "name": f"password-test-{secrets.token_hex(8)}",
        "address": input.address,
        "ssh_port": input.ssh_port,
        "ssh_user": input.ssh_user,
        "ssh_host_key": normalize_ssh_host_key(input.ssh_host_key),
        "ssh_auth_state": "pending",
    }
    authenticated, message = await asyncio.to_thread(test_ssh_password, node, input.password)
    audit_event(username, "host_password_test", input.address, "succeeded" if authenticated else "failed")
    return {"authenticated": authenticated, "message": message}


@app.post("/api/nodes/enroll", status_code=201)
async def enroll_node(input: NodeEnrollment, request: Request, username: Annotated[str, Depends(user)]):
    if input.install_controller_key and not enrollment_key_row():
        raise HTTPException(409, "Generate or import a controller-owned SSH key before enrolling this host")
    if input.auth_method == "controller_key" and not enrollment_key_row():
        raise HTTPException(409, "A controller-owned SSH key is required for key-based enrollment")
    host_key = normalize_ssh_host_key(input.ssh_host_key)
    requested_name = input.name.strip()
    temporary_name = requested_name or f"pending-{secrets.token_hex(8)}"
    try:
        with db() as con:
            cursor = con.execute(
                "INSERT INTO nodes(name,address,ssh_port,ssh_user,enabled,ssh_host_key,ssh_auth_state) VALUES (?,?,?,?,?,?, 'pending')",
                (temporary_name, input.address, input.ssh_port, input.ssh_user, 0, host_key),
            )
            node_id = cursor.lastrowid
            node = dict(con.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone())
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Node name already exists")
    node["enabled"] = bool(input.enabled)
    if input.auth_method == "password":
        run_id = launch_password_enrollment(node, input.password, input.install_controller_key, username, auto_name=not requested_name)
    else:
        run_id = launch_key_enrollment_probe(node, username, auto_name=not requested_name)
    return {"id": node_id, "run_id": run_id}


@app.get("/api/nodes")
async def nodes(_: Annotated[str, Depends(user)]):
    with db() as con:
        rows = con.execute("SELECT * FROM nodes ORDER BY name").fetchall()
        return [{**dict(row), "enabled": bool(row["enabled"]), "legacy_known_hosts_disabled": bool(row["legacy_known_hosts_disabled"])} for row in rows]


@app.post("/api/nodes", status_code=201)
async def create_node(input: Node, _: Annotated[str, Depends(user)]):
    try:
        with db() as con:
            cursor = con.execute(
                "INSERT INTO nodes(name,address,ssh_port,ssh_user,enabled) VALUES (?,?,?,?,?)",
                (input.name, input.address, input.ssh_port, input.ssh_user, input.enabled),
            )
        return {"id": cursor.lastrowid}
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Node name already exists")


@app.post("/api/nodes/{node_id}/controller-key")
async def install_controller_key(node_id: int, input: KeyInstall, request: Request, username: Annotated[str, Depends(user)]):
    with db() as con:
        row = con.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Node not found")
    node = dict(row)
    run_id = launch_password_enrollment(node, input.password, True, username)
    return {"run_id": run_id}


@app.put("/api/nodes/{node_id}")
async def update_node(node_id: int, input: NodeUpdate, username: Annotated[str, Depends(user)]):
    with db() as con:
        existing = con.execute("SELECT ssh_host_key FROM nodes WHERE id=?", (node_id,)).fetchone()
        if not existing:
            raise HTTPException(404, "Node not found")
        host_key = normalize_ssh_host_key(input.ssh_host_key) if input.ssh_host_key is not None else existing["ssh_host_key"]
        cursor = con.execute(
            "UPDATE nodes SET name=?,address=?,ssh_port=?,ssh_user=?,enabled=?,ssh_host_key=? WHERE id=?",
            (input.name, input.address, input.ssh_port, input.ssh_user, input.enabled, host_key, node_id),
        )
        if input.ssh_host_key is not None and host_key != existing["ssh_host_key"]:
            con.execute(
                "INSERT INTO audit_events(username,action,item_id,detail) VALUES (?,?,?,?)",
                (
                    username,
                    "host_ssh_host_key_replaced" if host_key else "host_ssh_host_key_validation_disabled",
                    str(node_id),
                    public_key_fingerprint(host_key) if host_key else "host key validation disabled",
                ),
            )
    return {"updated": True}


@app.post("/api/nodes/{node_id}/legacy-known-hosts/remove")
async def remove_legacy_known_hosts_record(node_id: int, username: Annotated[str, Depends(user)]):
    with db() as con:
        node = con.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
        if not node:
            raise HTTPException(404, "Node not found")
        if node["ssh_auth_state"] != "legacy":
            raise HTTPException(409, "This host is not using legacy SSH host-key trust")
        con.execute("UPDATE nodes SET legacy_known_hosts_disabled=1 WHERE id=?", (node_id,))
        con.execute(
            "INSERT INTO audit_events(username,action,item_id,detail) VALUES (?,?,?,?)",
            (username, "host_legacy_known_hosts_removed", str(node_id), "legacy host-key trust disabled for this host"),
        )
    return {"updated": True, "legacy_known_hosts_disabled": True}


@app.delete("/api/nodes/{node_id}")
async def delete_node(node_id: int, username: Annotated[str, Depends(user)], revoke_controller_key: bool = False, records_only: bool = False):
    with db() as con:
        node = con.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
        if not node:
            raise HTTPException(404, "Node not found")
        if con.execute("SELECT 1 FROM memberships WHERE node_id=?", (node_id,)).fetchone():
            raise HTTPException(409, "Remove this host from clusters before deleting its inventory record")
        if revoke_controller_key and records_only:
            raise HTTPException(422, "Choose either controller key revocation or records-only deletion")
        active, candidate = controller_key_rows()
        installed_key_id = node["ssh_key_id"] or node["candidate_key_id"]
        installed_key = next((key for key in (active, candidate) if key and key["key_id"] == installed_key_id), None)
        if revoke_controller_key:
            if not installed_key:
                raise HTTPException(409, "This host has no controller-owned SSH key available for revocation")
            payload = {"controller_public_key": installed_key["public_key"]}
            run_id = launch(
                "host-key-revoke", node["name"],
                lambda inv, variables_path: [
                    "ansible-playbook", "-i", str(inv), str(PLAYBOOKS / "host-revoke-controller-key.yml"), "--limit", node["name"],
                    "--private-key", active_ssh_key_path(), "--extra-vars", "@" + str(variables_path),
                ],
                variables=payload,
                context={"delete_node_after_revoke": node_id},
                inventory_nodes=[node_id],
            )
            audit_event(username, "host_controller_key_revocation", str(node_id), "key revocation requested before inventory deletion")
            return {"run_id": run_id}
        if installed_key and not records_only:
            raise HTTPException(409, "Choose controller key revocation or explicitly confirm records-only deletion")
        if records_only:
            con.execute(
                "INSERT INTO audit_events(username,action,item_id,detail) VALUES (?,?,?,?)",
                (username, "host_records_only_deletion", str(node_id), "controller key remains on host"),
            )
        cursor = con.execute("DELETE FROM nodes WHERE id=?", (node_id,))
    if not cursor.rowcount:
        raise HTTPException(404, "Node not found")
    return Response(status_code=204)


@app.post("/api/nodes/{node_id}/probe")
async def probe(node_id: int, _: Annotated[str, Depends(user)]):
    with db() as con:
        row = con.execute("SELECT name FROM nodes WHERE id=? AND enabled=1", (node_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Enabled node not found")
    return {"run_id": launch("probe", row["name"], lambda inv, _variables: ansible(inv, row["name"], "ping", ""))}


@app.post("/api/nodes/{node_id}/roles")
async def legacy_add_role(node_id: int, _: Annotated[str, Depends(user)]):
    raise HTTPException(410, "Use cluster-qualified assignments")


@app.delete("/api/nodes/{node_id}/roles/{role}")
async def legacy_remove_role(node_id: int, role: str, _: Annotated[str, Depends(user)]):
    raise HTTPException(410, "Use cluster-qualified assignments")


@app.get("/api/clusters")
async def clusters(_: Annotated[str, Depends(user)]):
    with db() as con:
        ids = [row["id"] for row in con.execute("SELECT id FROM clusters ORDER BY name")]
        return [cluster_record(con, cluster_id) for cluster_id in ids]


@app.post("/api/clusters", status_code=201)
async def create_cluster(input: ClusterInput, _: Annotated[str, Depends(user)]):
    slug = slugify(input.name)
    try:
        with db() as con:
            color = (input.theme_color or next_theme_color(con)).upper()
            cursor = con.execute(
                "INSERT INTO clusters(name,slug,ports_json,role_ports_json,secrets_json,observability_json,theme_color,desired_version,network_defaults_json,elasticsearch_settings_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    input.name, slug, input.ports.model_dump_json(),
                    json.dumps(input.role_ports.model_dump(by_alias=True), sort_keys=True),
                    seal_config(json.dumps({
                        "elastic_password": secrets.token_hex(24),
                        "kibana_password": secrets.token_hex(24),
                        "monitoring_password": secrets.token_hex(24),
                        "filebeat_password": secrets.token_hex(24),
                    })),
                    json.dumps(log_monitoring_config("", default_enabled=True), sort_keys=True),
                    color, input.desired_version, input.network_defaults.model_dump_json(), input.elasticsearch_settings.model_dump_json(),
                ),
            )
        return {"id": cursor.lastrowid}
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Cluster name already exists")


@app.get("/api/clusters/{cluster_id}")
async def get_cluster(cluster_id: int, _: Annotated[str, Depends(user)]):
    with db() as con:
        return cluster_record(con, cluster_id)


@app.put("/api/clusters/{cluster_id}")
async def update_cluster(cluster_id: int, input: ClusterInput, _: Annotated[str, Depends(user)]):
    slug = slugify(input.name)
    try:
        with db() as con:
            cluster_record(con, cluster_id)
            conflict = profile_conflict(con, cluster_id, input.role_ports.model_dump(by_alias=True))
            if conflict:
                raise HTTPException(409, conflict)
            cursor = con.execute(
                "UPDATE clusters SET name=?,slug=?,ports_json=?,role_ports_json=?,theme_color=?,desired_version=?,network_defaults_json=?,elasticsearch_settings_json=? WHERE id=?",
                (
                    input.name, slug, input.ports.model_dump_json(), json.dumps(input.role_ports.model_dump(by_alias=True), sort_keys=True), (input.theme_color or next_theme_color(con)).upper(),
                    input.desired_version, input.network_defaults.model_dump_json(), input.elasticsearch_settings.model_dump_json(), cluster_id,
                ),
            )
            if not cursor.rowcount:
                raise HTTPException(404, "Cluster not found")
        return {"updated": True}
    except sqlite3.IntegrityError:
        raise HTTPException(409, "Cluster name already exists")


@app.get("/api/clusters/{cluster_id}/log-monitoring")
async def get_cluster_log_monitoring(cluster_id: int, _: Annotated[str, Depends(user)]):
    with db() as con:
        return cluster_record(con, cluster_id)["log_monitoring"]


@app.put("/api/clusters/{cluster_id}/log-monitoring")
async def update_cluster_log_monitoring(cluster_id: int, input: LogMonitoringInput, username: Annotated[str, Depends(user)]):
    with db() as con:
        cluster = cluster_record(con, cluster_id)
        if active_cluster_operation(con, cluster["name"]):
            raise HTTPException(409, "Wait for the active cluster operation to finish")
        settings = {"filebeat_enabled": input.filebeat_enabled, "retention_days": FILEBEAT_RETENTION_DAYS}
        con.execute("UPDATE clusters SET observability_json=? WHERE id=?", (json.dumps(settings, sort_keys=True), cluster_id))
    audit_event(username, "cluster_log_monitoring_updated", str(cluster_id), "enabled" if input.filebeat_enabled else "disabled")
    return {"run_id": launch_filebeat_reconcile(cluster_id, username)}


@app.get("/api/clusters/{cluster_id}/versions")
async def get_cluster_versions(cluster_id: int, _: Annotated[str, Depends(user)]):
    with db() as con:
        details = version_details(con, cluster_id, include_candidates=False)
    try:
        with db() as con:
            cluster = cluster_record(con, cluster_id)
        details["available_versions"] = await asyncio.to_thread(available_versions, details["assignments"], cluster["log_monitoring"]["filebeat_enabled"])
    except HTTPException as error:
        details["registry_error"] = error.detail
    return details


@app.post("/api/clusters/{cluster_id}/versions/refresh")
async def refresh_cluster_versions(cluster_id: int, _: Annotated[str, Depends(user)]):
    with db() as con:
        cluster = cluster_record(con, cluster_id)
        if not cluster["assignments"]:
            raise HTTPException(422, "Assign workloads before refreshing component versions")
        if con.execute("SELECT 1 FROM runs WHERE status IN ('queued','running') AND target LIKE ?", (cluster["name"] + ":%",)).fetchone():
            raise HTTPException(409, "Wait for the active cluster operation to finish")
    return {"run_id": launch_commands(
        "version-probe", cluster["name"] + ":versions",
        lambda inv: [(probe_command(inv, cluster, assignment), {"assignment_id": assignment["id"]}) for assignment in cluster["assignments"]],
        record_observation,
    )}


@app.post("/api/clusters/{cluster_id}/versions/download")
async def download_cluster_versions(cluster_id: int, input: VersionTargetInput, _: Annotated[str, Depends(user)]):
    with db() as con:
        cluster = cluster_record(con, cluster_id)
        if con.execute("SELECT 1 FROM runs WHERE status IN ('queued','running') AND target LIKE ?", (cluster["name"] + ":%",)).fetchone():
            raise HTTPException(409, "Wait for the active cluster operation to finish")
    candidates = await asyncio.to_thread(available_versions, cluster["assignments"], cluster["log_monitoring"]["filebeat_enabled"])
    validate_version_target(cluster, input.target_version, candidates)
    images = {}
    for assignment in cluster["assignments"]:
        images[(assignment["node_name"], image_for_role(assignment["role"], input.target_version))] = True
        if assignment["role"] in METRICBEAT_ROLES:
            images[(assignment["node_name"], metricbeat_image(input.target_version))] = True
        if cluster["log_monitoring"]["filebeat_enabled"]:
            images[(assignment["node_name"], filebeat_image(input.target_version))] = True
    return {"run_id": launch_commands(
        "version-download", cluster["name"] + ":download:" + input.target_version,
        lambda inv: [(download_command(inv, node_name, image), {"node_name": node_name, "image": image}) for node_name, image in sorted(images)],
    )}


@app.post("/api/clusters/{cluster_id}/upgrades")
async def upgrade_cluster(cluster_id: int, input: VersionTargetInput, _: Annotated[str, Depends(user)]):
    with db() as con:
        cluster = cluster_record(con, cluster_id)
    candidates = await asyncio.to_thread(available_versions, cluster["assignments"], cluster["log_monitoring"]["filebeat_enabled"])
    return {"run_id": launch_upgrade(cluster_id, input.target_version, candidates)}


def configured_access_urls(cluster):
    urls = []
    members = {member["node_id"]: member for member in cluster["members"]}
    definitions = {
        "master": ("Elasticsearch API", "api", "https", "elasticsearch_http"),
        "hot": ("Elasticsearch API", "api", "https", "elasticsearch_http"),
        "warm": ("Elasticsearch API", "api", "https", "elasticsearch_http"),
        "ml": ("Elasticsearch API", "api", "https", "elasticsearch_http"),
        "ingest": ("Elasticsearch API", "api", "https", "elasticsearch_http"),
        "kibana": ("Kibana", "browser", "https", "kibana"),
        "fleet-server": ("Fleet Server", "api", "https", "fleet"),
        "logstash": ("Logstash API", "api", "http", "logstash_api"),
    }
    for assignment in cluster["assignments"]:
        definition = definitions.get(assignment["role"])
        if not definition or assignment["node_id"] not in members:
            continue
        label, audience, scheme, port_name = definition
        host = members[assignment["node_id"]]["user_address"]
        if not valid_ipv4(host):
            continue
        port = cluster["role_ports"][assignment["role"]][port_name]
        urls.append({
            "assignment_id": assignment["id"], "role": assignment["role"], "label": label,
            "audience": audience, "host": host, "port": port,
            "url": f"{scheme}://{host}:{port}",
        })
    return sorted(urls, key=lambda item: (item["audience"] != "browser", item["label"], item["host"], item["port"]))


TOPOLOGY_ES_ROLES = {
    "master": "master, remote_cluster_client",
    "hot": "data_hot, data_content, remote_cluster_client",
    "warm": "data_warm, data_content, remote_cluster_client",
    "ml": "ml, remote_cluster_client",
    "ingest": "ingest, remote_cluster_client",
    "coordinating": "coordinating only",
}


def topology_fit(value, width):
    value = str(value)
    return value if len(value) <= width else value[: max(0, width - 3)] + "..."


def topology_outer_line(value, width):
    return "|" + topology_fit(value, width).ljust(width) + "|"


def topology_role_box(lines, cluster, assignment, user_address, data_address, access, width):
    inner_width = width - 8
    config = assignment["config"]
    workload = f"ecp-{cluster['slug']}-{assignment['role']}-{assignment['node_id']}"
    details = [ROLE_SPECS[assignment["role"]]["label"], f"Name     : {workload}"]
    if assignment["role"] in TOPOLOGY_ES_ROLES:
        ports = cluster["role_ports"][assignment["role"]]
        details += [
            f"Roles    : {TOPOLOGY_ES_ROLES[assignment['role']]}",
            f"HTTP     : https://{user_address or 'not configured'}:{ports['elasticsearch_http']}",
            f"Transport: {data_address or 'not configured'}:{ports['elasticsearch_transport']}/tcp (TLS)",
        ]
    elif assignment["role"] == "elastic-agent":
        details.append("Connection: outbound TLS")
    elif access:
        details.append(f"URL      : {access['url']}")
    details += [
        f"CPU      : {config.get('cpu', '?')} cores  Memory: {config.get('memory', '?')}",
        f"Storage  : {config.get('storage_path', '?')}",
    ]
    lines.append("|  +" + "-" * (width - 6) + "+  |")
    for detail in details:
        lines.append("|  | " + topology_fit(detail, inner_width).ljust(inner_width) + " |  |")
    lines.append("|  +" + "-" * (width - 6) + "+  |")


def topology_transport_connector(lines, width, source, target, port):
    center = width // 2
    lines += [
        " " * center + "|",
        "  Elasticsearch transport",
        "  " + topology_fit(f"{source} -> {target}:{port}/tcp (TLS)", width - 2),
        " " * center + "v",
    ]


@app.get("/api/clusters/{cluster_id}/topology")
async def cluster_topology(cluster_id: int, _: Annotated[str, Depends(user)]):
    with db() as con:
        cluster = cluster_record(con, cluster_id)
    access_urls = configured_access_urls(cluster)
    width = 78
    lines = [f"Elastic Stack topology: {cluster['name']}"]
    if access_urls:
        lines += ["", "Configured user access:"]
        for access in access_urls:
            label = f"{access['label']} ({access['audience']})"
            lines.append(f"  {label:<29} {access['url']}")
    members = {member["node_id"]: member for member in cluster["members"]}
    grouped = {}
    for assignment in cluster["assignments"]:
        grouped.setdefault(assignment["node_id"], []).append(assignment)
    access_by_assignment = {access["assignment_id"]: access for access in access_urls}
    node_ids = [member["node_id"] for member in cluster["members"] if member["node_id"] in grouped]
    for index, node_id in enumerate(node_ids):
        assignments = grouped[node_id]
        member = members[node_id]
        lines += [
            "", "+" + "=" * width + "+",
            topology_outer_line(f" HOST: {member['name']}", width),
            topology_outer_line(f" Network : {member['network_mode'] or 'dedicated'}", width),
            topology_outer_line(f" User NIC: {member['user_interface'] or 'not configured'}  {member['user_address'] or 'not configured'}", width),
            topology_outer_line(f" Data NIC: {member['data_interface'] or 'not configured'}  {member['data_address'] or 'not configured'}", width),
            "+" + "=" * width + "+",
        ]
        for assignment in assignments:
            lines.append(topology_outer_line("", width))
            topology_role_box(lines, cluster, assignment, member["user_address"], member["data_address"], access_by_assignment.get(assignment["id"]), width)
        lines.append(topology_outer_line("", width))
        lines.append("+" + "=" * width + "+")
        if index + 1 < len(node_ids):
            next_assignments = grouped[node_ids[index + 1]]
            if (
                members[node_id]["data_address"]
                and members[node_ids[index + 1]]["data_address"]
                and any(item["role"] in TOPOLOGY_ES_ROLES for item in assignments)
                and any(item["role"] in TOPOLOGY_ES_ROLES for item in next_assignments)
            ):
                target = next(item for item in next_assignments if item["role"] in TOPOLOGY_ES_ROLES)
                topology_transport_connector(
                    lines,
                    width,
                    members[node_id]["data_address"],
                    members[node_ids[index + 1]]["data_address"],
                    cluster["role_ports"][target["role"]]["elasticsearch_transport"],
                )
    if not grouped:
        lines.append("No managed workloads are assigned.")
    return {"topology": "\n".join(lines) + "\n", "access_urls": access_urls}


@app.delete("/api/clusters/{cluster_id}", status_code=204)
async def delete_cluster(cluster_id: int, _: Annotated[str, Depends(user)]):
    with db() as con:
        count = con.execute("SELECT count(*) FROM cluster_assignments WHERE cluster_id=?", (cluster_id,)).fetchone()[0]
        if count:
            raise HTTPException(409, "Detach or purge all roles before deleting the cluster")
        cursor = con.execute("DELETE FROM clusters WHERE id=?", (cluster_id,))
    if not cursor.rowcount:
        raise HTTPException(404, "Cluster not found")
    from app import console
    console.invalidate_cluster_ca(cluster_id)


@app.post("/api/clusters/{cluster_id}/members", status_code=201)
async def add_member(cluster_id: int, input: MembershipInput, _: Annotated[str, Depends(user)]):
    validate_membership_network(input)
    with db() as con:
        cluster_record(con, cluster_id)
        node = con.execute("SELECT enabled FROM nodes WHERE id=?", (input.node_id,)).fetchone()
        if not node:
            raise HTTPException(404, "Node not found")
        if not node["enabled"]:
            raise HTTPException(422, "Enable the node before adding it to a cluster")
        try:
            columns = {row["name"] for row in con.execute("PRAGMA table_info(memberships)")}
            if "advertised_address" in columns:
                con.execute(
                    "INSERT INTO memberships(cluster_id,node_id,advertised_address,network_mode,data_interface,data_address,user_interface,user_address) VALUES (?,?,?,?,?,?,?,?)",
                    (cluster_id, input.node_id, input.user_address, input.network_mode, input.data_interface, input.data_address, input.user_interface, input.user_address),
                )
            else:
                con.execute(
                    "INSERT INTO memberships(cluster_id,node_id,network_mode,data_interface,data_address,user_interface,user_address) VALUES (?,?,?,?,?,?,?)",
                    (cluster_id, input.node_id, input.network_mode, input.data_interface, input.data_address, input.user_interface, input.user_address),
                )
        except sqlite3.IntegrityError:
            raise HTTPException(409, "Host is already in this cluster")
    return {"added": True}


@app.put("/api/clusters/{cluster_id}/members/{node_id}")
async def update_member(cluster_id: int, node_id: int, input: MembershipInput, _: Annotated[str, Depends(user)]):
    if input.node_id != node_id:
        raise HTTPException(422, "Membership node does not match the request path")
    validate_membership_network(input)
    with db() as con:
        cluster_record(con, cluster_id)
        cursor = con.execute("UPDATE memberships SET network_mode=?,data_interface=?,data_address=?,user_interface=?,user_address=? WHERE cluster_id=? AND node_id=?", (input.network_mode, input.data_interface, input.data_address, input.user_interface, input.user_address, cluster_id, node_id))
        if not cursor.rowcount:
            raise HTTPException(404, "Cluster membership not found")
    return {"updated": True}


@app.delete("/api/clusters/{cluster_id}/members/{node_id}", status_code=204)
async def remove_member(cluster_id: int, node_id: int, _: Annotated[str, Depends(user)]):
    with db() as con:
        if con.execute("SELECT 1 FROM cluster_assignments WHERE cluster_id=? AND node_id=?", (cluster_id, node_id)).fetchone():
            raise HTTPException(409, "Detach or purge the host roles first")
        con.execute("DELETE FROM memberships WHERE cluster_id=? AND node_id=?", (cluster_id, node_id))


@app.post("/api/clusters/{cluster_id}/assignments", status_code=201)
async def add_assignment(cluster_id: int, input: AssignmentInput, _: Annotated[str, Depends(user)]):
    validate_config(input.role, input.config)
    with db() as con:
        cluster_record(con, cluster_id)
        if not con.execute("SELECT 1 FROM memberships WHERE cluster_id=? AND node_id=?", (cluster_id, input.node_id)).fetchone():
            raise HTTPException(422, "Add the host to this cluster first")
        conflict = conflict_message(con, cluster_id, input.node_id, input.role)
        if conflict:
            raise HTTPException(409, conflict)
        try:
            cursor = con.execute(
                "INSERT INTO cluster_assignments(cluster_id,node_id,role,config_json) VALUES (?,?,?,?) "
                "ON CONFLICT(cluster_id,node_id,role) DO UPDATE SET config_json=excluded.config_json,state='active' RETURNING id",
                (cluster_id, input.node_id, input.role, seal_config(json.dumps(input.config))),
            )
            assignment_id = cursor.fetchone()["id"]
        except sqlite3.IntegrityError:
            raise HTTPException(409, "Assignment could not be created")
    return {"id": assignment_id}


@app.post("/api/clusters/{cluster_id}/workload-changes/apply")
async def apply_workload_changes(cluster_id: int, input: WorkloadChangeSet, _: Annotated[str, Depends(user)]):
    return {"run_id": launch_workload_change_batch(cluster_id, input)}


@app.put("/api/assignments/{assignment_id}/resources")
async def update_resources(assignment_id: int, input: ResourceInput, _: Annotated[str, Depends(user)]):
    update = input.model_dump()
    with db() as con:
        row = assignment_record(con, assignment_id)
        if row["operation_run_id"]:
            raise HTTPException(409, "This workload is already part of an active change set")
        require_ready_membership(row)
        previous = open_config(row["config_json"])
        config = {**previous, **update}
        validate_config(row["role"], config)
        con.execute("UPDATE cluster_assignments SET config_json=? WHERE id=?", (seal_config(json.dumps(config)), assignment_id))
        row = assignment_record(con, assignment_id)
        payload = cluster_payload(con, row)
        target = f"{row['cluster_name']}:{row['node_name']}:{row['role']}:resources"
        name = row["node_name"]
    return {"run_id": launch("resource-update", target, lambda inv, variables_path: reconcile_command(inv, variables_path, name), variables=payload, context={"rollback_assignment_id": assignment_id, "previous_config": previous, "filebeat_reconcile_cluster_id": row["cluster_id"]})}


@app.post("/api/assignments/{assignment_id}/apply")
async def apply_assignment(assignment_id: int, _: Annotated[str, Depends(user)]):
    with db() as con:
        row = assignment_record(con, assignment_id)
        if row["operation_run_id"]:
            raise HTTPException(409, "This workload is already part of an active change set")
        if not con.execute("SELECT enabled FROM nodes WHERE id=?", (row["node_id"],)).fetchone()["enabled"]:
            raise HTTPException(422, "Enable the host before applying a role")
        payload = cluster_payload(con, row)
        target = f"{row['cluster_name']}:{row['node_name']}:{row['role']}"
        name = row["node_name"]
    return {"run_id": launch("reconcile", target, lambda inv, variables_path: reconcile_command(inv, variables_path, name), variables=payload, context={"filebeat_reconcile_cluster_id": row["cluster_id"]})}


@app.delete("/api/assignments/{assignment_id}")
async def remove_assignment(assignment_id: int, _: Annotated[str, Depends(user)], mode: str = "detach"):
    if mode not in {"detach", "purge"}:
        raise HTTPException(422, "Removal mode must be detach or purge")
    with db() as con:
        row = assignment_record(con, assignment_id)
        if row["operation_run_id"]:
            raise HTTPException(409, "This workload is already part of an active change set")
        initial_master = con.execute(
            "SELECT id FROM cluster_assignments WHERE cluster_id=? AND role='master' ORDER BY id LIMIT 1",
            (row["cluster_id"],),
        ).fetchone()
        if row["role"] == "master" and initial_master and row["id"] == initial_master["id"] and con.execute(
            "SELECT 1 FROM cluster_assignments WHERE cluster_id=? AND id<>?",
            (row["cluster_id"], assignment_id),
        ).fetchone():
            raise HTTPException(409, "Detach or purge dependent cluster roles before removing the initial master")
        if mode == "detach":
            con.execute("DELETE FROM cluster_assignments WHERE id=?", (assignment_id,))
            return {"detached": True}
        payload = cluster_payload(con, row, "purge")
        target = f"{row['cluster_name']}:{row['node_name']}:{row['role']}:purge"
        name = row["node_name"]
    return {"run_id": launch("purge", target, lambda inv, variables_path: reconcile_command(inv, variables_path, name), variables=payload, context={"purge_assignment_id": assignment_id})}


@app.post("/api/hosts/initialize")
async def initialize_hosts(input: Targets, _: Annotated[str, Depends(user)]):
    with db() as con:
        selected = con.execute("SELECT name FROM nodes WHERE enabled=1 AND id IN (" + ",".join("?" * len(input.node_ids)) + ")", input.node_ids).fetchall()
    return {"run_ids": [launch("host-init", row["name"], lambda inv, _variables, name=row["name"]: [
        "ansible-playbook", "-i", str(inv), str(PLAYBOOKS / "host-init.yml"), "--limit", name, "--private-key", active_ssh_key_path(),
    ]) for row in selected]}


@app.get("/api/runs")
async def runs(_: Annotated[str, Depends(user)]):
    with db() as con:
        records = [dict(row) for row in con.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 100").fetchall()]
    return [{**record, "context_json": None, "events_token": run_events_token(record["id"])} for record in records]


@app.get("/api/runs/{run_id}/events")
async def events(run_id: int, request: Request, token: str = ""):
    header = request.headers.get("authorization", "")
    authorized = token_user(header[7:]) if header.startswith("Bearer ") else None
    if not authorized and not valid_run_events_token(token, run_id):
        raise HTTPException(401, "Authentication required")

    async def stream():
        old = None
        while True:
            with db() as con:
                row = con.execute("SELECT status,log FROM runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                yield "event: error\ndata: missing run\n\n"
                return
            if row["log"] != old:
                yield "event: log\ndata: " + json.dumps({"log": row["log"]}) + "\n\n"
                old = row["log"]
            if row["status"] in {"succeeded", "failed"}:
                yield "event: completed\ndata: " + json.dumps({"status": row["status"]}) + "\n\n"
                return
            await asyncio.sleep(1)

    return StreamingResponse(stream(), media_type="text/event-stream")


from app import console

app.include_router(console.router)


@app.get("/{frontend_path:path}", include_in_schema=False)
async def frontend(frontend_path: str):
    if frontend_path.startswith("api/"):
        raise HTTPException(404, "API endpoint not found")
    return FileResponse(STATIC_DIR / "index.html")
