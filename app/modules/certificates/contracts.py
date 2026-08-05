from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
import re
from typing import Mapping


@dataclass(frozen=True)
class CertificateMetadata:
    certificate_id: str
    workload: str
    subject: str
    san_addresses: tuple[str, ...]
    not_before: str
    not_after: str
    ca_fingerprint: str
    storage_path: str

    def public(self) -> dict[str, object]:
        return {
            "id": self.certificate_id,
            "workload": self.workload,
            "subject": self.subject,
            "san_addresses": list(self.san_addresses),
            "not_before": self.not_before,
            "not_after": self.not_after,
            "ca_fingerprint": self.ca_fingerprint,
            "storage_path": self.storage_path,
        }


@dataclass(frozen=True)
class RenewalPlan:
    certificate_id: str
    renew_before_days: int = 30
    preserve_ca: bool = True
    restart_required: bool = True


def renewal_due(not_after: str, *, renew_before_days: int = 30, now: datetime | None = None) -> bool:
    expiry = datetime.fromisoformat(not_after.replace("Z", "+00:00"))
    current = now or datetime.now(timezone.utc)
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    return expiry <= current + timedelta(days=renew_before_days)


TRUST_DOMAIN_KINDS = (
    "elasticsearch_transport",
    "elasticsearch_http",
    "kibana_http",
    "fleet_http",
)

DEFAULT_CERTIFICATE_POLICY = {
    "renew_before_days": 30,
    "critical_before_days": 14,
    "default_validity_days": 90,
    "issuer_validity_days": 365,
    "offline_root_validity_days": 3650,
    "renewal_mode": "approval_required",
    "ca_retirement_observation_days": 7,
}


class CertificateLifecycleError(RuntimeError):
    """Base error for public certificate lifecycle contracts."""


class CertificateNotFound(CertificateLifecycleError):
    """Raised when a public certificate asset is absent."""


class CertificateRevisionConflict(CertificateLifecycleError):
    """Raised when an optimistic policy update is stale."""


class CertificateValidationError(CertificateLifecycleError):
    """Raised when a lifecycle request violates its allowlisted policy."""


class CertificateCapabilityBlocked(CertificateLifecycleError):
    """Raised when an action requires a capability that is unavailable."""


_VERSION = re.compile(r"^(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)(?:[-+].*)?$")


def elastic_tls_capability(version: str) -> dict[str, object]:
    """Return the fail-closed TLS activation profile for one Elastic release.

    The first lifecycle release is deliberately PEM-only and restart-only. A
    future exact-release reload profile can extend this registry after a live
    proof; callers must not infer reload eligibility from the major version.
    """

    match = _VERSION.match((version or "").strip())
    if not match or int(match.group("major")) not in {8, 9}:
        return {
            "version": version,
            "supported": False,
            "reason_code": "unsupported_elastic_version",
            "format": "PEM",
            "reload_enabled": False,
            "restart_required": True,
            "profile": "unsupported",
        }
    major = int(match.group("major"))
    return {
        "version": version,
        "supported": True,
        "reason_code": None,
        "format": "PEM",
        "reload_enabled": False,
        "restart_required": True,
        "profile": f"elastic-{major}-pem-rolling-restart-v1",
    }


def validate_certificate_policy(value: Mapping[str, object]) -> dict[str, object]:
    """Normalize the public policy fields and reject unsafe automation.

    Certificate mutations require a later maintenance capability gate. The
    policy therefore accepts only manual, approval-required, and scheduled
    preview modes; ``automatic`` cannot accidentally enable mutation.
    """

    policy = dict(DEFAULT_CERTIFICATE_POLICY)
    for key in policy:
        if key in value:
            policy[key] = value[key]
    integer_keys = (
        "renew_before_days",
        "critical_before_days",
        "default_validity_days",
        "issuer_validity_days",
        "offline_root_validity_days",
        "ca_retirement_observation_days",
    )
    for key in integer_keys:
        try:
            policy[key] = int(policy[key])
        except (TypeError, ValueError) as error:
            raise CertificateValidationError(f"{key} must be an integer") from error
        if not 1 <= policy[key] <= 36500:
            raise CertificateValidationError(f"{key} must be between 1 and 36500")
    if policy["critical_before_days"] > policy["renew_before_days"]:
        raise CertificateValidationError("critical_before_days must not exceed renew_before_days")
    mode = str(policy["renewal_mode"])
    if mode not in {"manual", "approval_required", "scheduled"}:
        raise CertificateValidationError("renewal_mode must be manual, approval_required, or scheduled")
    policy["renewal_mode"] = mode
    return policy
