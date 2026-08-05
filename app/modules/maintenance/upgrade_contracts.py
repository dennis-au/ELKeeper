"""Plan-time contracts for guarded rolling upgrades.

The versions module owns registry discovery and image download.  This boundary
owns only immutable upgrade identity and preflight rules; it intentionally has
no restart or artifact mutation capability.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Iterable

from pydantic import BaseModel, ConfigDict, Field, model_validator


_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def _version(value: str) -> tuple[int, int, int]:
    match = _VERSION.fullmatch(str(value))
    if not match:
        raise ValueError("version must use MAJOR.MINOR.PATCH format")
    return tuple(int(item) for item in match.groups())


class UpgradeArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    assignment_id: int = Field(ge=1)
    node_id: int = Field(ge=1)
    role: str = Field(min_length=1, max_length=64)
    image: str = Field(min_length=1, max_length=512)
    version: str
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def image_matches_version(self) -> "UpgradeArtifact":
        _version(self.version)
        if not self.image.endswith(":" + self.version):
            raise ValueError("artifact image must end with its immutable version")
        return self


class UpgradeManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifacts: tuple[UpgradeArtifact, ...]
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


def build_upgrade_manifest(artifacts: Iterable[UpgradeArtifact]) -> UpgradeManifest:
    ordered = tuple(sorted(artifacts, key=lambda item: item.assignment_id))
    ids = [item.assignment_id for item in ordered]
    if len(ids) != len(set(ids)):
        raise ValueError("upgrade manifest contains duplicate assignment IDs")
    payload = [item.model_dump(mode="json") for item in ordered]
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return UpgradeManifest(artifacts=ordered, manifest_hash=digest)


class UpgradePreflight(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    cluster_healthy: bool
    master_eligible_available: int = Field(ge=0)
    snapshot_age_seconds: int | None = Field(default=None, ge=0)
    target_artifacts_ready: bool
    # These gates default to the legacy contract's optimistic values so older
    # callers remain source-compatible.  New maintenance planning callers
    # should always provide fresh evidence explicitly.
    observations_fresh: bool = True
    cluster_identity_matches: bool = True
    no_conflicting_operation: bool = True
    snapshot_verified: bool = True
    quorum_preserved: bool = True


def validate_upgrade_transition(
    *, current_version: str, target_version: str, preflight: UpgradePreflight,
) -> tuple[str, ...]:
    """Return stable blocking codes; an empty tuple is the only pass result."""

    current = _version(current_version)
    target = _version(target_version)
    blockers: list[str] = []
    if target < current:
        blockers.append("downgrade_not_supported")
        return tuple(blockers)
    if target[0] - current[0] > 1:
        blockers.append("major_jump_not_supported")
    if not preflight.cluster_healthy:
        blockers.append("cluster_health_required")
    if not preflight.observations_fresh:
        blockers.append("stale_runtime_observation")
    if not preflight.cluster_identity_matches:
        blockers.append("expected_cluster_identity_required")
    if not preflight.no_conflicting_operation:
        blockers.append("conflicting_operation")
    if not preflight.quorum_preserved:
        blockers.append("master_quorum_not_preserved")
    if not preflight.target_artifacts_ready:
        blockers.append("target_artifact_not_ready")
    if target[0] > current[0]:
        if preflight.master_eligible_available < 3:
            blockers.append("master_redundancy_required")
        if not preflight.snapshot_verified:
            blockers.append("snapshot_verification_required")
        if preflight.snapshot_age_seconds is None or preflight.snapshot_age_seconds > 24 * 60 * 60:
            blockers.append("recent_snapshot_required")
    return tuple(blockers)


__all__ = [
    "UpgradeArtifact",
    "UpgradeManifest",
    "UpgradePreflight",
    "build_upgrade_manifest",
    "validate_upgrade_transition",
]
