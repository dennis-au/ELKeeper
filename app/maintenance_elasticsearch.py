from __future__ import annotations

import re
import ssl
from collections.abc import Mapping
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Literal, Protocol
from urllib.parse import quote, urlsplit

import httpx
from pydantic import Field, SecretStr, field_validator, model_validator

from .maintenance_models import FrozenModel, MaintenanceBackend


ALLOCATION_ENABLE_SETTING = "cluster.routing.allocation.enable"
PLAN_ID_PATTERN = r"^[0-9a-f]{32}$"
NODE_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,256}$")
VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone")
    return value


def _path_segment(value: str, field_name: str = "node_id") -> str:
    if not NODE_ID_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name} contains unsupported characters")
    return quote(value, safe="")


class ElasticsearchMaintenanceError(RuntimeError):
    pass


class ElasticsearchRequestError(ElasticsearchMaintenanceError):
    def __init__(self, operation: str, category: str, status_code: int | None = None):
        self.operation = operation
        self.category = category
        self.status_code = status_code
        suffix = f" ({status_code})" if status_code is not None else ""
        super().__init__(f"Elasticsearch {operation} failed: {category}{suffix}")


class TransientAllocationPrecedence(ElasticsearchMaintenanceError):
    pass


class ShutdownBackendDisabled(ElasticsearchMaintenanceError):
    pass


class ElasticsearchClientConfig(FrozenModel):
    endpoint: str
    ca_path: str
    timeout_seconds: float = Field(default=10.0, gt=0, le=300)

    @field_validator("endpoint")
    @classmethod
    def endpoint_is_ca_verified_https_origin(cls, value):
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.port is None:
            raise ValueError("Elasticsearch maintenance requires an explicit HTTPS host and port")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Elasticsearch endpoint may not contain credentials, queries, or fragments")
        if parsed.path not in ("", "/"):
            raise ValueError("Elasticsearch endpoint must be an origin without an application path")
        return value.rstrip("/")

    @field_validator("ca_path")
    @classmethod
    def ca_path_is_absolute(cls, value):
        path = Path(value)
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("Elasticsearch CA path must be absolute and normalized")
        return str(path)


class ElasticsearchCredential(Protocol):
    def authorization_header(self) -> str:
        ...


class ApiKeyCredential(FrozenModel):
    value: SecretStr = Field(repr=False)

    @field_validator("value")
    @classmethod
    def value_is_non_empty(cls, value):
        raw = value.get_secret_value()
        if not raw or len(raw) > 8192 or any(character in raw for character in "\r\n"):
            raise ValueError("API key credential is invalid")
        return value

    def authorization_header(self) -> str:
        return f"ApiKey {self.value.get_secret_value()}"


class NodeShutdownCapability(FrozenModel):
    enabled: bool = False
    tested_versions: tuple[str, ...] = ()

    @field_validator("tested_versions", mode="before")
    @classmethod
    def versions_are_exact_and_deterministic(cls, value):
        versions = tuple(sorted(set(value or ())))
        if any(not VERSION_PATTERN.fullmatch(str(version)) for version in versions):
            raise ValueError("Shutdown capability versions must be exact Elasticsearch versions")
        return versions

    def require(self, version: str) -> None:
        if not self.enabled:
            raise ShutdownBackendDisabled("Elasticsearch Node Shutdown API backend is disabled")
        if version not in self.tested_versions:
            raise ShutdownBackendDisabled(
                "Elasticsearch Node Shutdown API backend is not verified for the observed version"
            )


class NodeShutdownStatus(str, Enum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    STALLED = "stalled"
    COMPLETE = "complete"


class NodeShutdownObservation(FrozenModel):
    node_id: str = Field(min_length=1, max_length=256)
    status: NodeShutdownStatus


class ElasticsearchMaintenanceClient:
    """CA-verified structured Elasticsearch client for maintenance observations.

    Authentication is attached as a client-level header. It is never embedded in
    an endpoint, query parameter, exception, command, or subprocess argument.
    Redirect following is disabled so credentials cannot be forwarded to another
    origin.
    """

    def __init__(
        self,
        config: ElasticsearchClientConfig,
        credential: ElasticsearchCredential,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        shutdown_capability: NodeShutdownCapability | None = None,
    ):
        self.config = config
        self.shutdown_capability = shutdown_capability or NodeShutdownCapability()
        self._ssl_context = ssl.create_default_context(cafile=config.ca_path)
        self._client = httpx.AsyncClient(
            base_url=config.endpoint + "/",
            verify=self._ssl_context,
            timeout=config.timeout_seconds,
            transport=transport,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": credential.authorization_header(),
            },
            follow_redirects=False,
            trust_env=False,
        )

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(endpoint={self.config.endpoint!r}, "
            f"ca_path={self.config.ca_path!r})"
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        body: Mapping[str, Any] | None = None,
        operation: str,
    ) -> Mapping[str, Any]:
        try:
            response = await self._client.request(method, path, params=params, json=body)
        except httpx.TimeoutException as error:
            raise ElasticsearchRequestError(operation, "timeout") from error
        except httpx.HTTPError as error:
            raise ElasticsearchRequestError(operation, "transport-error") from error
        if response.status_code < 200 or response.status_code >= 300:
            raise ElasticsearchRequestError(
                operation,
                "http-status",
                status_code=response.status_code,
            )
        try:
            payload = response.json()
        except (ValueError, UnicodeError) as error:
            raise ElasticsearchRequestError(operation, "invalid-json") from error
        if not isinstance(payload, Mapping):
            raise ElasticsearchRequestError(operation, "invalid-response-shape")
        return payload

    async def health(self) -> Mapping[str, Any]:
        return await self._request_json(
            "GET",
            "/_cluster/health",
            params={"level": "cluster"},
            operation="cluster-health",
        )

    async def settings(self) -> Mapping[str, Any]:
        return await self._request_json(
            "GET",
            "/_cluster/settings",
            params={"flat_settings": "true", "include_defaults": "false"},
            operation="cluster-settings-read",
        )

    async def put_settings(
        self,
        *,
        persistent: Mapping[str, str | None] | None = None,
        transient: Mapping[str, str | None] | None = None,
    ) -> Mapping[str, Any]:
        body: dict[str, Any] = {}
        if persistent is not None:
            body["persistent"] = dict(persistent)
        if transient is not None:
            body["transient"] = dict(transient)
        if not body:
            raise ValueError("At least one Elasticsearch settings layer is required")
        return await self._request_json(
            "PUT",
            "/_cluster/settings",
            body=body,
            operation="cluster-settings-write",
        )

    async def nodes_info(self, node_id: str | None = None) -> Mapping[str, Any]:
        path = "/_nodes" if node_id is None else f"/_nodes/{_path_segment(node_id)}"
        return await self._request_json("GET", path, operation="nodes-info")

    async def recovery(self, *, active_only: bool = False) -> Mapping[str, Any]:
        return await self._request_json(
            "GET",
            "/_recovery",
            params={
                "active_only": "true" if active_only else "false",
                "detailed": "true",
            },
            operation="recovery",
        )

    async def allocation_explain(
        self,
        *,
        index: str | None = None,
        shard: int | None = None,
        primary: bool | None = None,
    ) -> Mapping[str, Any]:
        selection = (index, shard, primary)
        if any(value is not None for value in selection) and not all(value is not None for value in selection):
            raise ValueError("Allocation explain selection requires index, shard, and primary together")
        body = None
        if index is not None:
            if not index or len(index) > 1024 or any(character in index for character in "\r\n"):
                raise ValueError("Allocation explain index is invalid")
            if not isinstance(shard, int) or isinstance(shard, bool) or shard < 0:
                raise ValueError("Allocation explain shard must be a non-negative integer")
            if not isinstance(primary, bool):
                raise ValueError("Allocation explain primary must be a boolean")
            body = {"index": index, "shard": shard, "primary": primary}
        return await self._request_json(
            "POST",
            "/_cluster/allocation/explain",
            body=body,
            operation="allocation-explain",
        )

    async def pending_tasks(self) -> Mapping[str, Any]:
        return await self._request_json(
            "GET",
            "/_cluster/pending_tasks",
            operation="pending-tasks",
        )

    async def register_restart_shutdown(
        self,
        *,
        node_id: str,
        node_version: str,
        reason: str,
        allocation_delay_seconds: int | None = None,
    ) -> Mapping[str, Any]:
        self.shutdown_capability.require(node_version)
        segment = _path_segment(node_id)
        if not reason or len(reason) > 512 or any(ord(character) < 32 for character in reason):
            raise ValueError("Shutdown reason is invalid")
        if allocation_delay_seconds is not None and (
            isinstance(allocation_delay_seconds, bool)
            or allocation_delay_seconds < 0
            or allocation_delay_seconds > 86400
        ):
            raise ValueError("Shutdown allocation delay is invalid")
        body: dict[str, Any] = {"type": "restart", "reason": reason}
        if allocation_delay_seconds is not None:
            body["allocation_delay"] = f"{allocation_delay_seconds}s"
        return await self._request_json(
            "PUT",
            f"/_nodes/{segment}/shutdown",
            body=body,
            operation="shutdown-register",
        )

    async def shutdown_status(
        self,
        *,
        node_id: str,
        node_version: str,
    ) -> NodeShutdownObservation | None:
        self.shutdown_capability.require(node_version)
        segment = _path_segment(node_id)
        payload = await self._request_json(
            "GET",
            f"/_nodes/{segment}/shutdown",
            operation="shutdown-status",
        )
        nodes = payload.get("nodes", ())
        if isinstance(nodes, Mapping):
            entries = list(nodes.values())
        elif isinstance(nodes, list):
            entries = nodes
        else:
            raise ElasticsearchRequestError("shutdown-status", "invalid-response-shape")
        for entry in entries:
            if not isinstance(entry, Mapping) or entry.get("node_id") != node_id:
                continue
            raw_status = str(entry.get("status", "")).strip().lower().replace("-", "_")
            try:
                status = NodeShutdownStatus(raw_status)
            except ValueError as error:
                raise ElasticsearchRequestError("shutdown-status", "unsupported-shutdown-status") from error
            return NodeShutdownObservation(node_id=node_id, status=status)
        return None

    async def delete_shutdown(self, *, node_id: str, node_version: str) -> Mapping[str, Any]:
        self.shutdown_capability.require(node_version)
        segment = _path_segment(node_id)
        return await self._request_json(
            "DELETE",
            f"/_nodes/{segment}/shutdown",
            operation="shutdown-delete",
        )


class SettingLayerValue(FrozenModel):
    present: bool
    value: str | None = None

    @model_validator(mode="after")
    def presence_matches_value(self):
        if self.present and self.value is None:
            raise ValueError("A present Elasticsearch setting requires its exact value")
        if not self.present and self.value is not None:
            raise ValueError("An absent Elasticsearch setting cannot have a value")
        return self


class AllocationSettingCapture(FrozenModel):
    setting: Literal["cluster.routing.allocation.enable"] = ALLOCATION_ENABLE_SETTING
    persistent: SettingLayerValue
    transient: SettingLayerValue
    captured_at: datetime

    @field_validator("captured_at")
    @classmethod
    def captured_at_is_aware(cls, value):
        return _aware(value, "captured_at")

    @property
    def effective_value(self) -> str:
        if self.transient.present:
            return self.transient.value or "all"
        if self.persistent.present:
            return self.persistent.value or "all"
        return "all"

    def restoration_payload(self) -> dict[str, dict[str, str | None]]:
        return {
            "persistent": {
                self.setting: self.persistent.value if self.persistent.present else None,
            },
            "transient": {
                self.setting: self.transient.value if self.transient.present else None,
            },
        }


def _layer_value(settings: Mapping[str, Any], layer_name: str) -> SettingLayerValue:
    layer = settings.get(layer_name, {})
    if layer is None:
        layer = {}
    if not isinstance(layer, Mapping):
        raise ValueError(f"Elasticsearch {layer_name} settings must be an object")
    if ALLOCATION_ENABLE_SETTING in layer:
        value = layer[ALLOCATION_ENABLE_SETTING]
    else:
        value: Any = layer
        for segment in ("cluster", "routing", "allocation", "enable"):
            if not isinstance(value, Mapping) or segment not in value:
                return SettingLayerValue(present=False)
            value = value[segment]
    if value is None:
        return SettingLayerValue(present=False)
    if not isinstance(value, str):
        raise ValueError(f"Elasticsearch {layer_name} allocation setting must be a string")
    return SettingLayerValue(present=True, value=value)


def capture_allocation_setting(
    settings: Mapping[str, Any],
    *,
    captured_at: datetime,
) -> AllocationSettingCapture:
    if not isinstance(settings, Mapping):
        raise ValueError("Elasticsearch settings response must be an object")
    return AllocationSettingCapture(
        persistent=_layer_value(settings, "persistent"),
        transient=_layer_value(settings, "transient"),
        captured_at=captured_at,
    )


class AllocationGuardPhase(str, Enum):
    CAPTURED = "captured"
    ACTIVE = "active"
    RESTORED = "restored"
    RECOVERY_REQUIRED = "recovery_required"


class AllocationCleanupTrigger(str, Enum):
    SUCCESS = "success"
    FAILURE = "failure"
    CANCEL = "cancel"
    RECOVERY = "recovery"


class AllocationGuardCheckpoint(FrozenModel):
    plan_id: str = Field(pattern=PLAN_ID_PATTERN)
    cluster_id: int = Field(ge=1)
    phase: AllocationGuardPhase
    captured: AllocationSettingCapture
    observed: AllocationSettingCapture | None = None
    updated_at: datetime

    @field_validator("updated_at")
    @classmethod
    def updated_at_is_aware(cls, value):
        return _aware(value, "updated_at")


class AllocationGuardCleanupResult(FrozenModel):
    status: Literal["restored", "recovery_required"]
    trigger: AllocationCleanupTrigger
    verified: bool
    checkpoint: AllocationGuardCheckpoint
    error_category: str | None = Field(default=None, max_length=128, pattern=r"^[a-z0-9-]+$")

    @model_validator(mode="after")
    def status_matches_checkpoint(self):
        if self.status == "restored" and (
            not self.verified or self.checkpoint.phase != AllocationGuardPhase.RESTORED
        ):
            raise ValueError("A restored result requires a verified restored checkpoint")
        if self.status == "recovery_required" and self.checkpoint.phase != AllocationGuardPhase.RECOVERY_REQUIRED:
            raise ValueError("A recovery result requires a recovery-required checkpoint")
        return self


class AllocationGuardApplyResult(FrozenModel):
    status: Literal["active", "failed", "recovery_required"]
    checkpoint: AllocationGuardCheckpoint
    cleanup: AllocationGuardCleanupResult | None = None
    error_category: str | None = Field(default=None, max_length=128, pattern=r"^[a-z0-9-]+$")

    @model_validator(mode="after")
    def result_is_consistent(self):
        if self.status == "active":
            if self.checkpoint.phase != AllocationGuardPhase.ACTIVE or self.cleanup is not None:
                raise ValueError("An active guard requires an active checkpoint without cleanup")
        elif self.cleanup is None:
            raise ValueError("A failed allocation guard requires cleanup evidence")
        return self


class AllocationGuardController:
    def __init__(
        self,
        client: ElasticsearchMaintenanceClient,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ):
        self.client = client
        self.clock = clock

    async def capture(self, *, plan_id: str, cluster_id: int) -> AllocationGuardCheckpoint:
        now = self.clock()
        captured = capture_allocation_setting(await self.client.settings(), captured_at=now)
        return AllocationGuardCheckpoint(
            plan_id=plan_id,
            cluster_id=cluster_id,
            phase=AllocationGuardPhase.CAPTURED,
            captured=captured,
            observed=captured,
            updated_at=now,
        )

    async def activate(self, checkpoint: AllocationGuardCheckpoint) -> AllocationGuardApplyResult:
        if checkpoint.phase != AllocationGuardPhase.CAPTURED:
            raise ValueError("Allocation guard activation requires a captured checkpoint")
        transient = checkpoint.captured.transient
        if transient.present and transient.value != "primaries":
            raise TransientAllocationPrecedence(
                "A transient allocation setting would override the persistent primaries guard"
            )
        try:
            await self.client.put_settings(
                persistent={ALLOCATION_ENABLE_SETTING: "primaries"},
            )
            observed = capture_allocation_setting(await self.client.settings(), captured_at=self.clock())
            if not (
                observed.persistent.present
                and observed.persistent.value == "primaries"
                and observed.effective_value == "primaries"
            ):
                return await self._activation_failed(
                    checkpoint,
                    error_category="allocation-guard-verification-failed",
                )
        except ElasticsearchRequestError as error:
            return await self._activation_failed(
                checkpoint,
                error_category=f"allocation-guard-{error.category}",
            )
        active = checkpoint.model_copy(
            update={
                "phase": AllocationGuardPhase.ACTIVE,
                "observed": observed,
                "updated_at": self.clock(),
            }
        )
        return AllocationGuardApplyResult(status="active", checkpoint=active)

    async def _activation_failed(
        self,
        checkpoint: AllocationGuardCheckpoint,
        *,
        error_category: str,
    ) -> AllocationGuardApplyResult:
        cleanup = await self.restore(checkpoint, trigger=AllocationCleanupTrigger.FAILURE)
        status = "failed" if cleanup.status == "restored" else "recovery_required"
        return AllocationGuardApplyResult(
            status=status,
            checkpoint=cleanup.checkpoint,
            cleanup=cleanup,
            error_category=error_category,
        )

    async def restore(
        self,
        checkpoint: AllocationGuardCheckpoint,
        *,
        trigger: AllocationCleanupTrigger,
    ) -> AllocationGuardCleanupResult:
        payload = checkpoint.captured.restoration_payload()
        request_error: str | None = None
        try:
            await self.client.put_settings(
                persistent=payload["persistent"],
                transient=payload["transient"],
            )
        except ElasticsearchRequestError as error:
            request_error = f"allocation-restoration-{error.category}"
        try:
            observed = capture_allocation_setting(await self.client.settings(), captured_at=self.clock())
        except (ElasticsearchRequestError, ValueError) as error:
            category = (
                f"allocation-restoration-{error.category}"
                if isinstance(error, ElasticsearchRequestError)
                else "allocation-restoration-invalid-response"
            )
            return self._recovery_result(
                checkpoint,
                trigger=trigger,
                observed=None,
                error_category=request_error or category,
            )
        verified = (
            observed.persistent == checkpoint.captured.persistent
            and observed.transient == checkpoint.captured.transient
        )
        if not verified:
            return self._recovery_result(
                checkpoint,
                trigger=trigger,
                observed=observed,
                error_category=request_error or "allocation-restoration-verification-failed",
            )
        restored = checkpoint.model_copy(
            update={
                "phase": AllocationGuardPhase.RESTORED,
                "observed": observed,
                "updated_at": self.clock(),
            }
        )
        return AllocationGuardCleanupResult(
            status="restored",
            trigger=trigger,
            verified=True,
            checkpoint=restored,
            error_category=request_error,
        )

    def _recovery_result(
        self,
        checkpoint: AllocationGuardCheckpoint,
        *,
        trigger: AllocationCleanupTrigger,
        observed: AllocationSettingCapture | None,
        error_category: str,
    ) -> AllocationGuardCleanupResult:
        recovery = checkpoint.model_copy(
            update={
                "phase": AllocationGuardPhase.RECOVERY_REQUIRED,
                "observed": observed,
                "updated_at": self.clock(),
            }
        )
        return AllocationGuardCleanupResult(
            status="recovery_required",
            trigger=trigger,
            verified=False,
            checkpoint=recovery,
            error_category=error_category,
        )


class ElasticsearchMaintenanceBackend(Protocol):
    kind: MaintenanceBackend
    uses_shutdown_api: bool


class DocumentedRollingBackend:
    kind = MaintenanceBackend.DOCUMENTED_ROLLING
    uses_shutdown_api = False

    def __init__(self, client: ElasticsearchMaintenanceClient):
        self.client = client
        self.allocation_guard = AllocationGuardController(client)


class NodeShutdownApiBackend:
    kind = MaintenanceBackend.NODE_SHUTDOWN_API
    uses_shutdown_api = True

    def __init__(
        self,
        client: ElasticsearchMaintenanceClient,
        *,
        capability: NodeShutdownCapability | None = None,
    ):
        self.client = client
        if capability is not None and capability != client.shutdown_capability:
            raise ValueError("Shutdown backend capability must match the CA-verified client capability")
        self.capability = client.shutdown_capability

    def _require(self, version: str) -> None:
        self.capability.require(version)
        self.client.shutdown_capability.require(version)

    async def prepare_restart(
        self,
        *,
        node_id: str,
        node_version: str,
        reason: str,
        allocation_delay_seconds: int | None = None,
    ) -> bool:
        self._require(node_version)
        await self.client.register_restart_shutdown(
            node_id=node_id,
            node_version=node_version,
            reason=reason,
            allocation_delay_seconds=allocation_delay_seconds,
        )
        return True

    async def status(self, *, node_id: str, node_version: str) -> NodeShutdownObservation | None:
        self._require(node_version)
        return await self.client.shutdown_status(node_id=node_id, node_version=node_version)

    async def cleanup_restart(self, *, node_id: str, node_version: str) -> bool:
        self._require(node_version)
        await self.client.delete_shutdown(node_id=node_id, node_version=node_version)
        return await self.client.shutdown_status(node_id=node_id, node_version=node_version) is None


def select_maintenance_backend(
    client: ElasticsearchMaintenanceClient,
    requested: MaintenanceBackend = MaintenanceBackend.DOCUMENTED_ROLLING,
    *,
    shutdown_capability: NodeShutdownCapability | None = None,
) -> ElasticsearchMaintenanceBackend:
    requested = MaintenanceBackend(requested)
    if requested == MaintenanceBackend.DOCUMENTED_ROLLING:
        return DocumentedRollingBackend(client)
    if requested == MaintenanceBackend.NODE_SHUTDOWN_API:
        return NodeShutdownApiBackend(client, capability=shutdown_capability)
    raise ShutdownBackendDisabled("No Elasticsearch maintenance backend is enabled")
