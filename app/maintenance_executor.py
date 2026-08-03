from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal, Union
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


EXECUTOR_SCHEMA_VERSION = 1
EXECUTOR_SIGNING_CONTEXT = "elkeeper-host-executor-v1"
MAINTENANCE_ROOT = PurePosixPath("/var/lib/elastic-control/maintenance")
EXECUTOR_SCRIPT_PATH = PurePosixPath("/usr/libexec/elkeeper/ecp-maintenance-resume")
EXECUTOR_UNIT_TEMPLATE_PATH = PurePosixPath("/etc/systemd/system/ecp-maintenance-resume@.service")
MAX_MANIFEST_LIFETIME_SECONDS = 86400
MAX_TOTAL_CHECK_SECONDS = 3600
OPERATION_ID_PATTERN = r"^[0-9a-f]{32}$"
BOOT_ID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
UNIT_PATTERN = r"^ecp-[a-z0-9][a-z0-9_.@-]{0,126}\.service$"
CHECK_ID_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,63}$"
ExecutorFailureReason = Literal[
    "boot_id_unavailable",
    "boot_id_unchanged",
    "check_failed",
    "executor_file_changed",
    "executor_file_invalid",
    "executor_path_invalid",
    "https_check_failed",
    "manifest_boot_id_invalid",
    "manifest_boot_transition_invalid",
    "manifest_check_invalid",
    "manifest_digest_mismatch",
    "manifest_envelope_invalid",
    "manifest_expired",
    "manifest_fields_invalid",
    "manifest_json_invalid",
    "manifest_key_mismatch",
    "manifest_node_invalid",
    "manifest_operation_mismatch",
    "manifest_path_invalid",
    "manifest_plan_invalid",
    "manifest_signature_invalid",
    "manifest_time_invalid",
    "manifest_timeout_invalid",
    "manifest_unit_invalid",
    "manifest_url_invalid",
    "manifest_version_invalid",
    "operation_id_invalid",
    "path_timeout",
    "protected_file_invalid",
    "protected_file_unavailable",
    "public_key_invalid",
    "result_already_exists",
    "result_too_large",
    "unexpected_failure",
    "unit_timeout",
]
ExecutorReason = Literal["completed"] | ExecutorFailureReason
ExecutorCheckError = Literal["https_check_failed", "path_timeout", "unit_timeout"]


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone")
    return value.astimezone(timezone.utc)


def validate_operation_id(value: str) -> str:
    if not re.fullmatch(OPERATION_ID_PATTERN, value):
        raise ValueError("operation_id must be a lowercase 32-character hexadecimal identifier")
    return value


def validate_managed_unit(value: str) -> str:
    if not re.fullmatch(UNIT_PATTERN, value) or value.startswith("ecp-maintenance-"):
        raise ValueError("unit must be a managed ecp-* workload service")
    return value


def _within(path: PurePosixPath, root: PurePosixPath) -> bool:
    return path == root or root in path.parents


def _absolute_clean_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts or str(path) != value:
        raise ValueError("path must be an absolute normalized POSIX path")
    return path


def validate_managed_reference_path(value: str) -> str:
    path = _absolute_clean_path(value)
    allowed_roots = (
        PurePosixPath("/etc/elastic-control"),
        MAINTENANCE_ROOT,
    )
    if path == PurePosixPath("/run/podman/podman.sock") or any(_within(path, root) for root in allowed_roots):
        return value
    raise ValueError("path is outside the controller-owned maintenance and cluster roots")


@dataclass(frozen=True)
class ExecutorPaths:
    operation_dir: PurePosixPath
    manifest: PurePosixPath
    public_key: PurePosixPath
    checkpoint: PurePosixPath
    result: PurePosixPath


def executor_paths(operation_id: str) -> ExecutorPaths:
    operation_id = validate_operation_id(operation_id)
    operation_dir = MAINTENANCE_ROOT / "operations" / operation_id
    return ExecutorPaths(
        operation_dir=operation_dir,
        manifest=operation_dir / "manifest.json",
        public_key=operation_dir / "signing-key.pem",
        checkpoint=operation_dir / "checkpoint.json",
        result=operation_dir / "result.json",
    )


def executor_instance_unit(operation_id: str) -> str:
    return f"ecp-maintenance-resume@{validate_operation_id(operation_id)}.service"


def require_operation_owned_path(value: str | PurePosixPath, operation_id: str) -> PurePosixPath:
    path = _absolute_clean_path(str(value))
    operation_dir = executor_paths(operation_id).operation_dir
    if not _within(path, operation_dir):
        raise ValueError("path is outside the controller-owned operation directory")
    return path


def validate_cleanup_paths(values: tuple[str, ...], operation_id: str) -> tuple[str, ...]:
    normalized = tuple(str(require_operation_owned_path(value, operation_id)) for value in values)
    if len(normalized) != len(set(normalized)):
        raise ValueError("cleanup paths must be unique")
    return tuple(sorted(normalized))


class UnitActiveCheck(FrozenModel):
    kind: Literal["systemd_unit_active"] = "systemd_unit_active"
    check_id: str = Field(pattern=CHECK_ID_PATTERN)
    unit: str
    timeout_seconds: int = Field(default=60, ge=1, le=900)

    @field_validator("unit")
    @classmethod
    def unit_is_managed(cls, value):
        return validate_managed_unit(value)


class PathExistsCheck(FrozenModel):
    kind: Literal["path_exists"] = "path_exists"
    check_id: str = Field(pattern=CHECK_ID_PATTERN)
    path: str
    timeout_seconds: int = Field(default=60, ge=1, le=900)

    @field_validator("path")
    @classmethod
    def path_is_managed(cls, value):
        return validate_managed_reference_path(value)


class LocalHttpsCheck(FrozenModel):
    kind: Literal["local_https"] = "local_https"
    check_id: str = Field(pattern=CHECK_ID_PATTERN)
    url: str
    ca_path: str
    curl_config_path: str | None = None
    timeout_seconds: int = Field(default=60, ge=1, le=900)

    @field_validator("url")
    @classmethod
    def url_is_local_and_protected(cls, value):
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.port is None:
            raise ValueError("local HTTPS checks require an explicit HTTPS host and port")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("local HTTPS checks may not contain credentials, queries, or fragments")
        if parsed.hostname != "localhost":
            try:
                ipaddress.ip_address(parsed.hostname)
            except ValueError as error:
                raise ValueError("local HTTPS checks require localhost or a literal IP address") from error
        return value

    @field_validator("ca_path", "curl_config_path")
    @classmethod
    def referenced_files_are_managed(cls, value):
        return validate_managed_reference_path(value) if value is not None else value


ExecutorCheck = Annotated[
    Union[UnitActiveCheck, PathExistsCheck, LocalHttpsCheck],
    Field(discriminator="kind"),
]


class HostExecutorManifest(FrozenModel):
    schema_version: Literal[1] = EXECUTOR_SCHEMA_VERSION
    signing_context: Literal["elkeeper-host-executor-v1"] = EXECUTOR_SIGNING_CONTEXT
    operation_id: str = Field(pattern=OPERATION_ID_PATTERN)
    plan_id: str = Field(pattern=OPERATION_ID_PATTERN)
    node_id: int = Field(ge=1)
    created_at: datetime
    expires_at: datetime
    expected_boot_transition: Literal["must_change"] = "must_change"
    pre_reboot_boot_id: str = Field(pattern=BOOT_ID_PATTERN)
    required_units: tuple[str, ...] = Field(default=(), max_length=100)
    checks: tuple[ExecutorCheck, ...] = Field(default=(), max_length=100)
    checkpoint_path: str
    result_path: str
    unit_wait_timeout_seconds: int = Field(default=600, ge=1, le=3600)
    poll_interval_seconds: int = Field(default=5, ge=1, le=60)

    @field_validator("created_at", "expires_at")
    @classmethod
    def timestamps_are_aware(cls, value, info):
        return _aware(value, info.field_name)

    @field_validator("operation_id", "plan_id")
    @classmethod
    def identifiers_are_safe(cls, value):
        return validate_operation_id(value)

    @field_validator("required_units")
    @classmethod
    def units_are_exact_and_ordered(cls, value):
        units = tuple(validate_managed_unit(item) for item in value)
        if len(units) != len(set(units)):
            raise ValueError("required_units must be unique")
        if units != tuple(sorted(units)):
            raise ValueError("required_units must use deterministic sorted order")
        return units

    @model_validator(mode="after")
    def validate_bounded_operation(self):
        lifetime = (self.expires_at - self.created_at).total_seconds()
        if lifetime <= 0 or lifetime > MAX_MANIFEST_LIFETIME_SECONDS:
            raise ValueError("executor manifest lifetime must be positive and no longer than 24 hours")
        expected_paths = executor_paths(self.operation_id)
        if self.checkpoint_path != str(expected_paths.checkpoint):
            raise ValueError("checkpoint_path must be the controller-owned operation checkpoint")
        if self.result_path != str(expected_paths.result):
            raise ValueError("result_path must be the controller-owned operation result")
        check_ids = [item.check_id for item in self.checks]
        if len(check_ids) != len(set(check_ids)):
            raise ValueError("executor check IDs must be unique")
        unit_set = set(self.required_units)
        for check in self.checks:
            if isinstance(check, UnitActiveCheck) and check.unit not in unit_set:
                raise ValueError("systemd unit checks must reference a previously-running required unit")
        total_timeout = self.unit_wait_timeout_seconds + sum(item.timeout_seconds for item in self.checks)
        if total_timeout > MAX_TOTAL_CHECK_SECONDS:
            raise ValueError("executor checks exceed the bounded one-hour execution window")
        return self


class ExecutorUnitResult(FrozenModel):
    unit: str
    active: bool

    @field_validator("unit")
    @classmethod
    def unit_is_managed(cls, value):
        return validate_managed_unit(value)


class ExecutorCheckResult(FrozenModel):
    check_id: str = Field(pattern=CHECK_ID_PATTERN)
    passed: bool
    error_category: ExecutorCheckError | None = None

    @model_validator(mode="after")
    def error_matches_outcome(self):
        if self.passed == (self.error_category is not None):
            raise ValueError("passed checks must omit an error category and failed checks must include one")
        return self


class HostExecutorResult(FrozenModel):
    schema_version: Literal[1] = EXECUTOR_SCHEMA_VERSION
    signing_context: Literal["elkeeper-host-executor-v1"] = EXECUTOR_SIGNING_CONTEXT
    operation_id: str = Field(pattern=OPERATION_ID_PATTERN)
    plan_id: str | None = Field(default=None, pattern=OPERATION_ID_PATTERN)
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: Literal["complete", "recovery_required"]
    reason_code: ExecutorReason
    pre_reboot_boot_id: str | None = Field(default=None, pattern=BOOT_ID_PATTERN)
    observed_boot_id: str | None = Field(default=None, pattern=BOOT_ID_PATTERN)
    units: tuple[ExecutorUnitResult, ...] = Field(default=(), max_length=100)
    checks: tuple[ExecutorCheckResult, ...] = Field(default=(), max_length=100)
    started_at: datetime
    completed_at: datetime

    @field_validator("operation_id", "plan_id")
    @classmethod
    def identifiers_are_safe(cls, value):
        return validate_operation_id(value) if value is not None else value

    @field_validator("started_at", "completed_at")
    @classmethod
    def timestamps_are_aware(cls, value, info):
        return _aware(value, info.field_name)

    @model_validator(mode="after")
    def validate_result(self):
        if self.completed_at < self.started_at:
            raise ValueError("completed_at cannot precede started_at")
        unit_names = [item.unit for item in self.units]
        check_ids = [item.check_id for item in self.checks]
        if len(unit_names) != len(set(unit_names)) or len(check_ids) != len(set(check_ids)):
            raise ValueError("executor result entries must be unique")
        if self.state == "complete" and (
            self.reason_code != "completed" or any(not item.active for item in self.units) or any(not item.passed for item in self.checks)
        ):
            raise ValueError("a complete executor result requires every bounded check to pass")
        return self


class ExecutorSignature(FrozenModel):
    algorithm: Literal["ed25519"] = "ed25519"
    key_id: str = Field(pattern=r"^SHA256:[A-Za-z0-9_-]{43}$")
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    value: str = Field(pattern=r"^[A-Za-z0-9_-]{86}$")


class SignedHostExecutorManifest(FrozenModel):
    manifest: HostExecutorManifest
    signature: ExecutorSignature


class SignedHostExecutorResult(FrozenModel):
    result: HostExecutorResult
    signature: ExecutorSignature


class SignatureVerificationError(ValueError):
    pass


def _canonical_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): _canonical_value(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"Unsupported executor signing value: {type(value).__name__}")


def canonical_executor_json(value: Any) -> str:
    return json.dumps(
        _canonical_value(value), ensure_ascii=True, separators=(",", ":"), sort_keys=True, allow_nan=False,
    )


def _private_key(value: bytes | Ed25519PrivateKey, password: bytes | None = None) -> Ed25519PrivateKey:
    if isinstance(value, Ed25519PrivateKey):
        return value
    if len(value) == 32:
        return Ed25519PrivateKey.from_private_bytes(value)
    loaded = serialization.load_pem_private_key(value, password=password)
    if not isinstance(loaded, Ed25519PrivateKey):
        raise TypeError("executor signatures require an Ed25519 private key")
    return loaded


def _public_key(value: bytes | Ed25519PublicKey) -> Ed25519PublicKey:
    if isinstance(value, Ed25519PublicKey):
        return value
    if len(value) == 32:
        return Ed25519PublicKey.from_public_bytes(value)
    try:
        loaded = serialization.load_pem_public_key(value)
    except ValueError:
        loaded = serialization.load_ssh_public_key(value)
    if not isinstance(loaded, Ed25519PublicKey):
        raise TypeError("executor signatures require an Ed25519 public key")
    return loaded


def executor_key_id(value: bytes | Ed25519PublicKey) -> str:
    key = _public_key(value)
    der = key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    encoded = base64.urlsafe_b64encode(hashlib.sha256(der).digest()).decode("ascii").rstrip("=")
    return "SHA256:" + encoded


def executor_public_key_pem(value: bytes | Ed25519PrivateKey | Ed25519PublicKey) -> bytes:
    if isinstance(value, Ed25519PrivateKey):
        value = value.public_key()
    elif isinstance(value, bytes):
        value = _public_key(value)
    return value.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)


def _signature(value: BaseModel, private_key: bytes | Ed25519PrivateKey, password: bytes | None = None) -> ExecutorSignature:
    key = _private_key(private_key, password=password)
    payload = canonical_executor_json(value).encode("utf-8")
    return ExecutorSignature(
        key_id=executor_key_id(key.public_key()),
        payload_sha256=hashlib.sha256(payload).hexdigest(),
        value=base64.urlsafe_b64encode(key.sign(payload)).decode("ascii").rstrip("="),
    )


def _verify(value: BaseModel, signature: ExecutorSignature, public_key: bytes | Ed25519PublicKey) -> None:
    key = _public_key(public_key)
    if signature.key_id != executor_key_id(key):
        raise SignatureVerificationError("executor signing key identity does not match")
    payload = canonical_executor_json(value).encode("utf-8")
    if signature.payload_sha256 != hashlib.sha256(payload).hexdigest():
        raise SignatureVerificationError("executor payload digest does not match")
    try:
        encoded = signature.value + "=" * (-len(signature.value) % 4)
        raw_signature = base64.b64decode(encoded.encode("ascii"), altchars=b"-_", validate=True)
        if len(raw_signature) != 64:
            raise ValueError("invalid Ed25519 signature length")
        key.verify(raw_signature, payload)
    except (InvalidSignature, ValueError, TypeError) as error:
        raise SignatureVerificationError("executor signature verification failed") from error


def sign_executor_manifest(
    manifest: HostExecutorManifest,
    private_key: bytes | Ed25519PrivateKey,
    *,
    password: bytes | None = None,
) -> SignedHostExecutorManifest:
    return SignedHostExecutorManifest(manifest=manifest, signature=_signature(manifest, private_key, password))


def verify_executor_manifest(
    envelope: SignedHostExecutorManifest,
    public_key: bytes | Ed25519PublicKey,
    *,
    now: datetime | None = None,
) -> HostExecutorManifest:
    _verify(envelope.manifest, envelope.signature, public_key)
    current = _aware(now or datetime.now(timezone.utc), "now")
    if current >= envelope.manifest.expires_at:
        raise SignatureVerificationError("executor manifest has expired")
    return envelope.manifest


def sign_executor_result(
    result: HostExecutorResult,
    private_key: bytes | Ed25519PrivateKey,
    *,
    password: bytes | None = None,
) -> SignedHostExecutorResult:
    return SignedHostExecutorResult(result=result, signature=_signature(result, private_key, password))


def verify_executor_result(
    envelope: SignedHostExecutorResult,
    public_key: bytes | Ed25519PublicKey,
) -> HostExecutorResult:
    _verify(envelope.result, envelope.signature, public_key)
    return envelope.result
