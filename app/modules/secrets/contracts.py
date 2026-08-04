"""Secret catalog and audited reveal primitives."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping

from app.modules.platform import redact


@dataclass(frozen=True)
class SecretMetadata:
    item_id: str
    category: str
    label: str
    updated_at: str | None = None
    masked: bool = True

    def public(self) -> dict[str, object]:
        return {"id": self.item_id, "category": self.category, "label": self.label, "updated_at": self.updated_at, "masked": True}


@dataclass(frozen=True)
class RevealGrant:
    token: str
    cluster_id: int
    expires_at: datetime
    purpose: str

    def valid_for(self, cluster_id: int, purpose: str, now: datetime | None = None) -> bool:
        current = now or datetime.now(timezone.utc)
        return self.cluster_id == cluster_id and self.purpose == purpose and current < self.expires_at


def redact_secret_payload(value: Mapping[str, object]) -> dict[str, object]:
    result = redact(dict(value))
    return result if isinstance(result, dict) else {}
