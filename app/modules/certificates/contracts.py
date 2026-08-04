from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta


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
