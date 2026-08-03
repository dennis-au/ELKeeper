from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from enum import Enum
from pathlib import PurePosixPath
from typing import Any, Protocol

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import Field, SecretStr, field_validator, model_validator

from .maintenance_elasticsearch import (
    ApiKeyCredential,
    ElasticsearchClientConfig,
    ElasticsearchMaintenanceClient,
    NodeShutdownCapability,
)
from .maintenance_executor import (
    BOOT_ID_PATTERN,
    HostExecutorResult,
    SignedHostExecutorManifest,
    executor_instance_unit,
    executor_paths,
    executor_public_key_pem,
    validate_cleanup_paths,
    validate_managed_unit,
    validate_operation_id,
    verify_executor_manifest,
)
from .maintenance_models import FrozenModel
from .maintenance_post_return import (
    CleanupProof,
    CleanupStatus,
    ExecutorCleanupTarget,
    NodeIdentityExpectation,
    NodeIdentityObservation,
    ServiceBudgetExpectation,
    ServiceBudgetObservation,
    ShardRecoveryObservation,
)
from .maintenance_reboot import (
    ExecutorDiscovery,
    ExecutorDiscoveryState,
    ExecutorStageReceipt,
    InvocationAmbiguous,
    RebootInvocationReceipt,
    ReconnectObservation,
    SshDisconnectObservation,
)


EXECUTOR_STAGE_PLAYBOOK = "host-maintenance-executor-stage.yml"
MAX_EXECUTOR_MANIFEST_BYTES = 262144
MAX_EXECUTOR_STATE_BYTES = 65536


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone")
    return value.astimezone(timezone.utc)


class RuntimeMutationDisabled(RuntimeError):
    pass


class RuntimeIdentityError(RuntimeError):
    pass


class RemoteOutcomeUnknown(RuntimeError):
    """The transport lost contact before it could prove the remote outcome."""


class ExecutionOutcome(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    AMBIGUOUS = "ambiguous"


class MaintenanceRuntimeFlags(FrozenModel):
    executor_staging_enabled: bool = False
    reboot_enabled: bool = False
    cleanup_enabled: bool = False


class PlaybookExecutionRequest(FrozenModel):
    node_id: int = Field(ge=1)
    playbook: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}\.yml$")
    variables: Mapping[str, Any]
    timeout_seconds: int = Field(default=300, ge=1, le=3600)

    @field_validator("variables")
    @classmethod
    def variables_are_redacted_and_serializable(cls, value):
        forbidden = ("password", "passphrase", "private_key", "token", "secret", "credential")
        for key in value:
            normalized = str(key).lower()
            if any(item in normalized for item in forbidden):
                raise ValueError("maintenance playbook variables may not contain credentials")
        json.dumps(value, ensure_ascii=True, allow_nan=False)
        return dict(value)


class ExecutionReceipt(FrozenModel):
    outcome: ExecutionOutcome
    invocation_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    observed_at: datetime
    error_category: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{0,127}$")

    @field_validator("observed_at")
    @classmethod
    def observed_at_is_aware(cls, value):
        return _aware(value, "observed_at")

    @model_validator(mode="after")
    def outcome_matches_error(self):
        if self.outcome == ExecutionOutcome.SUCCEEDED and self.error_category is not None:
            raise ValueError("successful execution receipts cannot include an error")
        if self.outcome != ExecutionOutcome.SUCCEEDED and self.error_category is None:
            raise ValueError("failed or ambiguous execution receipts require an error category")
        return self


class RebootRequestReceipt(FrozenModel):
    operation_id: str
    invocation_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    outcome: ExecutionOutcome
    observed_at: datetime
    error_category: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{0,127}$")

    @field_validator("operation_id")
    @classmethod
    def operation_is_safe(cls, value):
        return validate_operation_id(value)

    @field_validator("observed_at")
    @classmethod
    def observed_at_is_aware(cls, value):
        return _aware(value, "observed_at")

    @model_validator(mode="after")
    def outcome_matches_error(self):
        if self.outcome == ExecutionOutcome.SUCCEEDED and self.error_category is not None:
            raise ValueError("successful reboot receipts cannot include an error")
        if self.outcome != ExecutionOutcome.SUCCEEDED and self.error_category is None:
            raise ValueError("failed or ambiguous reboot receipts require an error category")
        return self


class ManagedFileObservation(FrozenModel):
    path: str
    exists: bool
    regular: bool = False
    symlink: bool = False
    owner_uid: int | None = Field(default=None, ge=0)
    mode: int | None = Field(default=None, ge=0, le=0o7777)
    content: bytes | None = Field(default=None, max_length=MAX_EXECUTOR_MANIFEST_BYTES)

    @model_validator(mode="after")
    def absent_files_have_no_metadata(self):
        path = PurePosixPath(self.path)
        if not path.is_absolute() or ".." in path.parts or str(path) != self.path:
            raise ValueError("managed file observation path must be absolute and normalized")
        if not self.exists and any(
            value is not None for value in (self.owner_uid, self.mode, self.content)
        ):
            raise ValueError("absent files cannot contain metadata or content")
        return self

    def require_secure_regular(self, *, maximum_bytes: int) -> bytes:
        if not self.exists:
            raise FileNotFoundError(self.path)
        if not self.regular or self.symlink or self.owner_uid != 0 or self.mode != 0o600:
            raise RuntimeIdentityError("executor artifact ownership or mode is invalid")
        if self.content is None or not 0 < len(self.content) <= maximum_bytes:
            raise RuntimeIdentityError("executor artifact content is unavailable or oversized")
        return self.content


class ControllerMaintenanceIO(Protocol):
    """Controller-owned SSH/Ansible port.

    Its implementation must resolve inventory, the active controller SSH key,
    and pinned/explicitly-unpinned host-key policy. It must not log request
    variables or managed-file content.
    """

    async def run_playbook(self, request: PlaybookExecutionRequest) -> ExecutionReceipt: ...

    async def request_reboot(self, *, node_id: int, operation_id: str) -> RebootRequestReceipt: ...

    async def wait_for_disconnect(
        self, *, node_id: int, invocation_id: str,
    ) -> SshDisconnectObservation: ...

    async def wait_for_reconnect(self, *, node_id: int) -> ReconnectObservation: ...

    async def wait_for_ssh(self, *, node_id: int, timeout_seconds: int) -> bool: ...

    async def read_boot_id(self, *, node_id: int) -> str | None: ...

    async def observe_file(
        self, *, node_id: int, path: str, maximum_bytes: int,
    ) -> ManagedFileObservation: ...

    async def podman_socket_ready(self, *, node_id: int) -> bool: ...

    async def quadlet_generator_ready(self, *, node_id: int) -> bool: ...

    async def generated_units(self, *, node_id: int, units: tuple[str, ...]) -> frozenset[str]: ...

    async def unit_states(self, *, node_id: int, units: tuple[str, ...]) -> Mapping[str, bool]: ...

    async def endpoint_ready(self, *, node_id: int, endpoint_ref: str) -> bool: ...

    async def cleanup_executor(
        self, *, node_id: int, unit: str, paths: tuple[str, ...],
    ) -> CleanupProof: ...


class ControllerManagedHostRuntime:
    """Validating adapter for reboot orchestration and post-return host checks."""

    def __init__(
        self,
        *,
        node_id: int,
        io: ControllerMaintenanceIO,
        executor_public_key: bytes | Ed25519PublicKey,
        flags: MaintenanceRuntimeFlags | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ):
        if node_id < 1:
            raise ValueError("node_id must be positive")
        self.node_id = node_id
        self.io = io
        self.executor_public_key = executor_public_key
        self.executor_public_key_pem = executor_public_key_pem(executor_public_key).decode("ascii")
        self.flags = flags or MaintenanceRuntimeFlags()
        self.clock = clock

    async def stage(self, envelope: SignedHostExecutorManifest) -> ExecutorStageReceipt:
        if not self.flags.executor_staging_enabled:
            raise RuntimeMutationDisabled("maintenance executor staging is disabled")
        manifest = verify_executor_manifest(envelope, self.executor_public_key, now=self.clock())
        if manifest.node_id != self.node_id:
            raise RuntimeIdentityError("executor manifest targets a different inventory node")
        checkpoint = {
            "schema_version": manifest.schema_version,
            "operation_id": manifest.operation_id,
            "state": "staged",
            "manifest_hash": envelope.signature.payload_sha256,
            "observed_at": self.clock().isoformat().replace("+00:00", "Z"),
        }
        request = PlaybookExecutionRequest(
            node_id=self.node_id,
            playbook=EXECUTOR_STAGE_PLAYBOOK,
            variables={
                "maintenance_executor_stage_enabled": True,
                "maintenance_executor_operation_id": manifest.operation_id,
                "maintenance_executor_manifest_json": envelope.model_dump_json(),
                "maintenance_executor_public_key_pem": self.executor_public_key_pem,
                "maintenance_executor_checkpoint_json": json.dumps(
                    checkpoint,
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            },
        )
        receipt = await self.io.run_playbook(request)
        if receipt.outcome == ExecutionOutcome.AMBIGUOUS:
            raise RemoteOutcomeUnknown("executor staging outcome is ambiguous")
        if receipt.outcome != ExecutionOutcome.SUCCEEDED:
            raise RuntimeError("executor staging failed")
        return ExecutorStageReceipt(
            operation_id=manifest.operation_id,
            manifest_hash=envelope.signature.payload_sha256,
            acknowledged=True,
            staged_at=receipt.observed_at,
        )

    async def invoke_reboot(self, *, node_id: int, operation_id: str) -> RebootInvocationReceipt:
        self._require_node(node_id)
        operation_id = validate_operation_id(operation_id)
        if not self.flags.reboot_enabled:
            raise RuntimeMutationDisabled("maintenance reboot execution is disabled")
        try:
            receipt = await self.io.request_reboot(node_id=node_id, operation_id=operation_id)
        except RemoteOutcomeUnknown as error:
            raise InvocationAmbiguous("reboot request outcome is ambiguous") from error
        if receipt.operation_id != operation_id:
            raise RuntimeIdentityError("reboot acknowledgement operation identity does not match")
        if receipt.outcome == ExecutionOutcome.AMBIGUOUS:
            raise InvocationAmbiguous("reboot request outcome is ambiguous")
        return RebootInvocationReceipt(
            operation_id=operation_id,
            invocation_id=receipt.invocation_id,
            acknowledged=receipt.outcome == ExecutionOutcome.SUCCEEDED,
            acknowledged_at=receipt.observed_at,
        )

    async def wait_for_disconnect(
        self, *, node_id: int, invocation_id: str,
    ) -> SshDisconnectObservation:
        self._require_node(node_id)
        return await self.io.wait_for_disconnect(node_id=node_id, invocation_id=invocation_id)

    async def wait_for_reconnect(self, *, node_id: int) -> ReconnectObservation:
        self._require_node(node_id)
        observation = await self.io.wait_for_reconnect(node_id=node_id)
        if observation.connected and observation.boot_id is not None:
            self._validate_boot_id(observation.boot_id)
        return observation

    async def discover(self, *, operation_id: str) -> ExecutorDiscovery:
        operation_id = validate_operation_id(operation_id)
        paths = executor_paths(operation_id)
        manifest_observation = await self.io.observe_file(
            node_id=self.node_id,
            path=str(paths.manifest),
            maximum_bytes=MAX_EXECUTOR_MANIFEST_BYTES,
        )
        if not manifest_observation.exists:
            return ExecutorDiscovery(
                operation_id=operation_id,
                state=ExecutorDiscoveryState.NOT_FOUND,
                observed_at=self.clock(),
            )
        try:
            envelope = self._validated_staged_manifest(manifest_observation, operation_id)
        except Exception:
            return ExecutorDiscovery(
                operation_id=operation_id,
                state=ExecutorDiscoveryState.RECOVERY_REQUIRED,
                observed_at=self.clock(),
            )
        result_observation = await self.io.observe_file(
            node_id=self.node_id,
            path=str(paths.result),
            maximum_bytes=MAX_EXECUTOR_STATE_BYTES,
        )
        if result_observation.exists:
            try:
                result = self._validated_result(result_observation, envelope)
            except Exception:
                return ExecutorDiscovery(
                    operation_id=operation_id,
                    state=ExecutorDiscoveryState.RECOVERY_REQUIRED,
                    observed_at=self.clock(),
                )
            state = (
                ExecutorDiscoveryState.COMPLETE
                if result.state == "complete"
                else ExecutorDiscoveryState.RECOVERY_REQUIRED
            )
            return ExecutorDiscovery(
                operation_id=operation_id,
                state=state,
                observed_at=self.clock(),
                result=result if state == ExecutorDiscoveryState.COMPLETE else None,
            )
        checkpoint_observation = await self.io.observe_file(
            node_id=self.node_id,
            path=str(paths.checkpoint),
            maximum_bytes=MAX_EXECUTOR_STATE_BYTES,
        )
        try:
            checkpoint_state = self._checkpoint_state(
                checkpoint_observation,
                operation_id=operation_id,
                manifest_hash=envelope.signature.payload_sha256,
            )
        except Exception:
            return ExecutorDiscovery(
                operation_id=operation_id,
                state=ExecutorDiscoveryState.RECOVERY_REQUIRED,
                observed_at=self.clock(),
            )
        state = {
            "staged": ExecutorDiscoveryState.STAGED,
            "manifest_verified": ExecutorDiscoveryState.RUNNING,
            "boot_transition_verified": ExecutorDiscoveryState.RUNNING,
            "required_units_ready": ExecutorDiscoveryState.RUNNING,
        }.get(checkpoint_state, ExecutorDiscoveryState.RECOVERY_REQUIRED)
        return ExecutorDiscovery(operation_id=operation_id, state=state, observed_at=self.clock())

    async def import_result(self, operation_id: str) -> HostExecutorResult:
        operation_id = validate_operation_id(operation_id)
        paths = executor_paths(operation_id)
        manifest = await self.io.observe_file(
            node_id=self.node_id,
            path=str(paths.manifest),
            maximum_bytes=MAX_EXECUTOR_MANIFEST_BYTES,
        )
        envelope = self._validated_staged_manifest(manifest, operation_id)
        result = await self.io.observe_file(
            node_id=self.node_id,
            path=str(paths.result),
            maximum_bytes=MAX_EXECUTOR_STATE_BYTES,
        )
        return self._validated_result(result, envelope)

    async def wait_for_ssh(self, node_id: int, timeout_seconds: int) -> bool:
        self._require_node(node_id)
        return await self.io.wait_for_ssh(node_id=node_id, timeout_seconds=timeout_seconds)

    async def read_boot_id(self, node_id: int) -> str | None:
        self._require_node(node_id)
        value = await self.io.read_boot_id(node_id=node_id)
        return self._validate_boot_id(value) if value is not None else None

    async def podman_socket_ready(self, node_id: int) -> bool:
        self._require_node(node_id)
        return await self.io.podman_socket_ready(node_id=node_id)

    async def quadlet_generator_ready(self, node_id: int) -> bool:
        self._require_node(node_id)
        return await self.io.quadlet_generator_ready(node_id=node_id)

    async def generated_units(self, node_id: int, units: tuple[str, ...]) -> frozenset[str]:
        self._require_node(node_id)
        expected = self._validated_units(units)
        observed = await self.io.generated_units(node_id=node_id, units=expected)
        return frozenset(unit for unit in observed if unit in expected)

    async def unit_states(self, node_id: int, units: tuple[str, ...]) -> Mapping[str, bool]:
        self._require_node(node_id)
        expected = self._validated_units(units)
        observed = await self.io.unit_states(node_id=node_id, units=expected)
        return {unit: observed[unit] is True for unit in expected if unit in observed}

    async def endpoint_ready(self, node_id: int, endpoint_ref: str) -> bool:
        self._require_node(node_id)
        if not endpoint_ref or len(endpoint_ref) > 128:
            raise ValueError("endpoint_ref is invalid")
        return await self.io.endpoint_ready(node_id=node_id, endpoint_ref=endpoint_ref)

    async def cleanup_executor(self, target: ExecutorCleanupTarget) -> CleanupProof:
        if not self.flags.cleanup_enabled:
            raise RuntimeMutationDisabled("maintenance executor cleanup is disabled")
        expected_unit = executor_instance_unit(target.operation_id)
        paths = validate_cleanup_paths(target.paths, target.operation_id)
        if target.unit != expected_unit:
            return CleanupProof(status=CleanupStatus.OWNERSHIP_REJECTED)
        return await self.io.cleanup_executor(
            node_id=self.node_id,
            unit=expected_unit,
            paths=paths,
        )

    def _validated_staged_manifest(
        self,
        observation: ManagedFileObservation,
        operation_id: str,
    ) -> SignedHostExecutorManifest:
        raw = observation.require_secure_regular(maximum_bytes=MAX_EXECUTOR_MANIFEST_BYTES)
        envelope = SignedHostExecutorManifest.model_validate_json(raw)
        # Historical discovery verifies the signature at the signed creation time;
        # expiry controls execution, not later evidence import.
        manifest = verify_executor_manifest(
            envelope,
            self.executor_public_key,
            now=envelope.manifest.created_at,
        )
        if manifest.operation_id != operation_id or manifest.node_id != self.node_id:
            raise RuntimeIdentityError("staged executor manifest identity does not match")
        return envelope

    @staticmethod
    def _validated_result(
        observation: ManagedFileObservation,
        envelope: SignedHostExecutorManifest,
    ) -> HostExecutorResult:
        raw = observation.require_secure_regular(maximum_bytes=MAX_EXECUTOR_STATE_BYTES)
        result = HostExecutorResult.model_validate_json(raw)
        manifest = envelope.manifest
        expected_hash = envelope.signature.payload_sha256
        if (
            result.operation_id != manifest.operation_id
            or result.plan_id != manifest.plan_id
            or result.manifest_hash != expected_hash
            or result.pre_reboot_boot_id != manifest.pre_reboot_boot_id
            or result.started_at < manifest.created_at
            or result.observed_boot_id is None
            or result.observed_boot_id == manifest.pre_reboot_boot_id
        ):
            raise RuntimeIdentityError("executor result identity does not match the staged manifest")
        expected_units = set(manifest.required_units)
        if {item.unit for item in result.units} != expected_units:
            raise RuntimeIdentityError("executor result unit evidence is incomplete")
        expected_checks = {item.check_id for item in manifest.checks}
        if {item.check_id for item in result.checks} != expected_checks:
            raise RuntimeIdentityError("executor result check evidence is incomplete")
        return result

    @staticmethod
    def _checkpoint_state(
        observation: ManagedFileObservation,
        *,
        operation_id: str,
        manifest_hash: str,
    ) -> str:
        raw = observation.require_secure_regular(maximum_bytes=MAX_EXECUTOR_STATE_BYTES)
        payload = json.loads(raw)
        required = {"schema_version", "operation_id", "state", "manifest_hash", "observed_at"}
        if not isinstance(payload, dict) or set(payload) != required:
            raise RuntimeIdentityError("executor checkpoint shape is invalid")
        if (
            payload["schema_version"] != 1
            or payload["operation_id"] != operation_id
            or payload["manifest_hash"] != manifest_hash
            or not isinstance(payload["state"], str)
        ):
            raise RuntimeIdentityError("executor checkpoint identity does not match")
        _aware(datetime.fromisoformat(str(payload["observed_at"]).replace("Z", "+00:00")), "observed_at")
        return payload["state"]

    def _require_node(self, node_id: int) -> None:
        if node_id != self.node_id:
            raise RuntimeIdentityError("runtime adapter is bound to a different inventory node")

    @staticmethod
    def _validate_boot_id(value: str) -> str:
        from re import fullmatch

        if not fullmatch(BOOT_ID_PATTERN, value):
            raise RuntimeIdentityError("host boot ID is invalid")
        return value

    @staticmethod
    def _validated_units(units: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(validate_managed_unit(unit) for unit in units)
        if len(normalized) != len(set(normalized)):
            raise ValueError("managed units must be unique")
        return normalized


class ElasticsearchRuntimeConnection(FrozenModel):
    endpoint: str
    ca_path: str
    api_key: SecretStr = Field(repr=False)
    timeout_seconds: float = Field(default=10.0, gt=0, le=300)
    shutdown_capability: NodeShutdownCapability = Field(default_factory=NodeShutdownCapability)


class ElasticsearchConnectionResolver(Protocol):
    def resolve(self, cluster_id: int) -> ElasticsearchRuntimeConnection: ...


class RuntimeElasticsearchClient(ElasticsearchMaintenanceClient):
    async def root_info(self) -> Mapping[str, Any]:
        return await self._request_json("GET", "/", operation="cluster-identity")


ElasticsearchClientBuilder = Callable[
    [ElasticsearchClientConfig, ApiKeyCredential, httpx.AsyncBaseTransport | None, NodeShutdownCapability],
    RuntimeElasticsearchClient,
]


def _default_client_builder(
    config: ElasticsearchClientConfig,
    credential: ApiKeyCredential,
    transport: httpx.AsyncBaseTransport | None,
    capability: NodeShutdownCapability,
) -> RuntimeElasticsearchClient:
    return RuntimeElasticsearchClient(
        config,
        credential,
        transport=transport,
        shutdown_capability=capability,
    )


class CaVerifiedElasticsearchClientPool:
    """Lazily builds one CA-verified client per configured cluster."""

    def __init__(
        self,
        resolver: ElasticsearchConnectionResolver,
        *,
        builder: ElasticsearchClientBuilder = _default_client_builder,
        transport_factory: Callable[[int], httpx.AsyncBaseTransport | None] | None = None,
    ):
        self.resolver = resolver
        self.builder = builder
        self.transport_factory = transport_factory
        self._clients: dict[int, RuntimeElasticsearchClient] = {}

    def get(self, cluster_id: int) -> RuntimeElasticsearchClient:
        if cluster_id < 1:
            raise ValueError("cluster_id must be positive")
        existing = self._clients.get(cluster_id)
        if existing is not None:
            return existing
        material = self.resolver.resolve(cluster_id)
        config = ElasticsearchClientConfig(
            endpoint=material.endpoint,
            ca_path=material.ca_path,
            timeout_seconds=material.timeout_seconds,
        )
        credential = ApiKeyCredential(value=material.api_key)
        transport = self.transport_factory(cluster_id) if self.transport_factory else None
        client = self.builder(config, credential, transport, material.shutdown_capability)
        self._clients[cluster_id] = client
        return client

    async def aclose(self) -> None:
        clients = tuple(self._clients.values())
        self._clients.clear()
        for client in clients:
            await client.aclose()


class ServiceAvailabilityProvider(Protocol):
    async def available(self, expectation: ServiceBudgetExpectation) -> int: ...


class ElasticsearchPostReturnAdapter:
    """Collects structured, CA-verified cluster evidence after host return."""

    def __init__(
        self,
        clients: CaVerifiedElasticsearchClientPool,
        service_availability: ServiceAvailabilityProvider,
    ):
        self.clients = clients
        self.service_availability = service_availability

    async def node_identity(
        self,
        expectation: NodeIdentityExpectation,
    ) -> NodeIdentityObservation | None:
        client = self.clients.get(expectation.cluster_id)
        root = await client.root_info()
        nodes_payload = await client.nodes_info(expectation.persistent_node_id)
        nodes = nodes_payload.get("nodes")
        if not isinstance(nodes, Mapping):
            raise ValueError("Elasticsearch nodes-info response is invalid")
        node = nodes.get(expectation.persistent_node_id)
        if node is None:
            return None
        if not isinstance(node, Mapping):
            raise ValueError("Elasticsearch node identity response is invalid")
        cluster_uuid = root.get("cluster_uuid")
        node_name = node.get("name")
        version = node.get("version")
        if not all(isinstance(value, str) and value for value in (cluster_uuid, node_name, version)):
            raise ValueError("Elasticsearch identity evidence is incomplete")
        return NodeIdentityObservation(
            persistent_node_id=expectation.persistent_node_id,
            node_name=node_name,
            version=version,
            cluster_uuid=cluster_uuid,
        )

    async def shard_recovery(self, cluster_id: int) -> ShardRecoveryObservation:
        client = self.clients.get(cluster_id)
        recovery = await client.recovery(active_only=True)
        health = await client.health()
        return ShardRecoveryObservation(
            active_recoveries=_count_recovery_entries(recovery),
            initializing_shards=_non_negative_integer(health, "initializing_shards"),
            relocating_shards=_non_negative_integer(health, "relocating_shards"),
            unassigned_primaries=_non_negative_integer(health, "unassigned_primary_shards"),
        )

    async def service_budget(
        self,
        expectation: ServiceBudgetExpectation,
    ) -> ServiceBudgetObservation:
        available = await self.service_availability.available(expectation)
        return ServiceBudgetObservation(available=available)

    async def cluster_health(self, cluster_id: int) -> str:
        status = (await self.clients.get(cluster_id).health()).get("status")
        if status not in ("green", "yellow", "red"):
            raise ValueError("Elasticsearch health status is invalid")
        return str(status)


def _non_negative_integer(payload: Mapping[str, Any], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"Elasticsearch {key} evidence is invalid")
    return value


def _count_recovery_entries(payload: Mapping[str, Any]) -> int:
    total = 0
    for index_value in payload.values():
        if not isinstance(index_value, Mapping):
            continue
        shards = index_value.get("shards", ())
        if not isinstance(shards, list):
            raise ValueError("Elasticsearch recovery shard evidence is invalid")
        total += len(shards)
    return total
