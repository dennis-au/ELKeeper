"""Thin HTTP routes for certificate lifecycle read models and gated previews."""

from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .contracts import (
    CertificateCapabilityBlocked,
    CertificateLifecycleError,
    CertificateNotFound,
    CertificateRevisionConflict,
    CertificateValidationError,
)
from .service import CertificateLifecycleService


class CertificatePolicyInput(BaseModel):
    expected_revision: int = Field(ge=1)
    renew_before_days: int | None = Field(default=None, ge=1, le=36500)
    critical_before_days: int | None = Field(default=None, ge=1, le=36500)
    default_validity_days: int | None = Field(default=None, ge=1, le=36500)
    issuer_validity_days: int | None = Field(default=None, ge=1, le=36500)
    offline_root_validity_days: int | None = Field(default=None, ge=1, le=36500)
    renewal_mode: str | None = None
    ca_retirement_observation_days: int | None = Field(default=None, ge=1, le=36500)


class PreviewExecutionInput(BaseModel):
    preview_hash: str = Field(min_length=32, max_length=128)


class ExternalTrustConsumerInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trust_domain_id: str = Field(min_length=1, max_length=128)
    consumer_kind: str = Field(min_length=3, max_length=64)
    description: str = Field(min_length=3, max_length=200)
    verification_method: str = Field(min_length=3, max_length=64)


def _http_error(error: CertificateLifecycleError) -> HTTPException:
    if isinstance(error, CertificateNotFound):
        return HTTPException(404, str(error))
    if isinstance(error, CertificateRevisionConflict):
        return HTTPException(409, str(error))
    if isinstance(error, CertificateCapabilityBlocked):
        return HTTPException(409, str(error))
    return HTTPException(422, str(error))


def build_router(*, service: CertificateLifecycleService, user_dependency: Callable) -> APIRouter:
    """Build public routes; lifecycle behavior remains in the certificate module."""

    router = APIRouter()

    @router.get("/api/clusters/{cluster_id}/certificates")
    async def list_certificates(cluster_id: int, _: str = Depends(user_dependency)):
        try:
            return service.list_assets(cluster_id)
        except CertificateLifecycleError as error:
            raise _http_error(error) from error

    @router.get("/api/certificates/{certificate_id}")
    async def certificate_detail(certificate_id: str, _: str = Depends(user_dependency)):
        try:
            return service.asset_detail(certificate_id)
        except CertificateLifecycleError as error:
            raise _http_error(error) from error

    @router.get("/api/clusters/{cluster_id}/certificate-policy")
    async def certificate_policy(cluster_id: int, _: str = Depends(user_dependency)):
        try:
            return service.policy(cluster_id)
        except CertificateLifecycleError as error:
            raise _http_error(error) from error

    @router.put("/api/clusters/{cluster_id}/certificate-policy")
    async def update_certificate_policy(
        cluster_id: int,
        input: CertificatePolicyInput,
        username: str = Depends(user_dependency),
    ):
        try:
            return service.update_policy(cluster_id, input.model_dump(exclude_none=True), username=username)
        except CertificateLifecycleError as error:
            raise _http_error(error) from error

    @router.get("/api/clusters/{cluster_id}/certificate-compatibility")
    async def certificate_compatibility(cluster_id: int, _: str = Depends(user_dependency)):
        try:
            return service.compatibility(cluster_id)
        except CertificateLifecycleError as error:
            raise _http_error(error) from error

    @router.get("/api/clusters/{cluster_id}/certificate-trust-consumers")
    async def certificate_trust_consumers(cluster_id: int, _: str = Depends(user_dependency)):
        try:
            return service.trust_consumers(cluster_id)
        except CertificateLifecycleError as error:
            raise _http_error(error) from error

    @router.post("/api/clusters/{cluster_id}/certificate-trust-consumers")
    async def declare_certificate_trust_consumer(
        cluster_id: int,
        input: ExternalTrustConsumerInput,
        username: str = Depends(user_dependency),
    ):
        try:
            return service.declare_external_consumer(cluster_id, input.model_dump(), username=username)
        except CertificateLifecycleError as error:
            raise _http_error(error) from error

    @router.get("/api/clusters/{cluster_id}/certificate-operations")
    async def certificate_operations(cluster_id: int, _: str = Depends(user_dependency)):
        try:
            return service.operations(cluster_id)
        except CertificateLifecycleError as error:
            raise _http_error(error) from error

    @router.get("/api/certificate-operations/{operation_id}")
    async def certificate_operation(operation_id: str, _: str = Depends(user_dependency)):
        try:
            return service.operation_detail(operation_id)
        except CertificateLifecycleError as error:
            raise _http_error(error) from error

    @router.get("/api/certificate-operations/{operation_id}/report")
    async def certificate_operation_report(operation_id: str, _: str = Depends(user_dependency)):
        try:
            operation = service.operation_detail(operation_id)
            return {"operation": operation, "redacted": True}
        except CertificateLifecycleError as error:
            raise _http_error(error) from error

    @router.post("/api/clusters/{cluster_id}/certificates/refresh")
    async def refresh_certificates(cluster_id: int, username: str = Depends(user_dependency)):
        try:
            return await service.refresh(cluster_id, username=username)
        except CertificateLifecycleError as error:
            raise _http_error(error) from error

    @router.post("/api/certificates/{certificate_id}/renewal-preview")
    async def renewal_preview(certificate_id: str, username: str = Depends(user_dependency)):
        try:
            return service.renewal_preview(certificate_id, username=username)
        except CertificateLifecycleError as error:
            raise _http_error(error) from error

    @router.post("/api/clusters/{cluster_id}/ca-rotation-preview")
    async def ca_rotation_preview(cluster_id: int, username: str = Depends(user_dependency)):
        try:
            return service.ca_rotation_preview(cluster_id, username=username)
        except CertificateLifecycleError as error:
            raise _http_error(error) from error

    @router.post("/api/certificate-operations/{operation_id}/execute")
    async def execute_certificate_operation(
        operation_id: str,
        input: PreviewExecutionInput,
        _: str = Depends(user_dependency),
    ):
        try:
            operation = service.operation_detail(operation_id)
            if operation["request_hash"] != input.preview_hash:
                raise CertificateValidationError("Certificate preview has changed; create a new preview")
            service.require_execution_capability(operation_id)
        except CertificateLifecycleError as error:
            raise _http_error(error) from error
        # This endpoint remains deliberately unreachable until the shared
        # maintenance executor is proven. Keep a defensive response for a
        # future capability implementation rather than starting a side effect.
        raise HTTPException(409, "Certificate execution adapter is not installed")

    return router


__all__ = ["CertificatePolicyInput", "ExternalTrustConsumerInput", "PreviewExecutionInput", "build_router"]
