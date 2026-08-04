"""Version cache and guarded-operation contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .contracts import version_key


@dataclass(frozen=True)
class VersionTarget:
    value: str
    stable: bool = True
    available: bool = True


@dataclass(frozen=True)
class DownloadPlan:
    cluster_id: int
    target_version: str
    images: tuple[str, ...]


class UpgradeGuard:
    """Pure policy gate for no-downgrade and major-jump checks."""

    @staticmethod
    def validate(current: str, target: str, *, healthy: bool, snapshot_recent: bool, master_eligible: int) -> tuple[bool, str]:
        current_key = version_key(current)
        target_key = version_key(target)
        if target_key < current_key:
            return False, "downgrade is not allowed"
        if target_key[0] > current_key[0] + 1:
            return False, "upgrade skips more than one major version"
        if not healthy:
            return False, "cluster health is not ready"
        if target_key[0] > current_key[0] and not snapshot_recent:
            return False, "a recent snapshot is required for a major upgrade"
        if master_eligible < 3:
            return False, "rolling Elasticsearch upgrades require three master-eligible nodes"
        return True, "ready"


def stable_targets(values: Iterable[str]) -> tuple[VersionTarget, ...]:
    return tuple(VersionTarget(value) for value in sorted(set(values), key=version_key, reverse=True))
