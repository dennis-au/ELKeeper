"""Host storage discovery and safe workload-mount eligibility."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Callable


VIRTUAL_STORAGE_TYPES = frozenset({
    "autofs", "bpf", "cgroup", "cgroup2", "configfs", "debugfs", "devpts", "devtmpfs",
    "efivarfs", "fusectl", "hugetlbfs", "mqueue", "nsfs", "overlay", "proc", "pstore",
    "ramfs", "rpc_pipefs", "securityfs", "selinuxfs", "sysfs", "tmpfs", "tracefs",
})
UNSAFE_STORAGE_MOUNT_PREFIXES = (
    "/boot", "/dev", "/etc", "/lib", "/lib64", "/proc", "/root", "/run", "/sbin", "/sys",
    "/usr", "/var/lib/containers", "/var/lib/kubelet",
)


def storage_mount_entries(filesystems: object) -> Iterator[dict[str, Any]]:
    for filesystem in filesystems or []:
        if not isinstance(filesystem, dict):
            continue
        yield filesystem
        yield from storage_mount_entries(filesystem.get("children"))


def storage_mount_eligibility(
    target: str,
    fstype: str,
    options: str,
    available_bytes: int,
    *,
    valid_storage_path: Callable[[str], bool],
) -> tuple[bool, str]:
    option_set = {option.strip() for option in options.split(",")}
    if fstype in VIRTUAL_STORAGE_TYPES:
        return False, "virtual filesystem"
    if "rw" not in option_set:
        return False, "read-only mount"
    if available_bytes <= 0:
        return False, "no free space"
    if target == "/":
        return True, ""
    if not valid_storage_path(target):
        return False, "system mount"
    if any(target == prefix or target.startswith(prefix + "/") for prefix in UNSAFE_STORAGE_MOUNT_PREFIXES):
        return False, "controller-reserved mount"
    return True, ""


def storage_mounts(payload: dict[str, Any], *, valid_storage_path: Callable[[str], bool]) -> list[dict[str, Any]]:
    mounts: list[dict[str, Any]] = []
    seen_targets: set[str] = set()
    for entry in storage_mount_entries(payload.get("filesystems")):
        target = entry.get("target")
        if not isinstance(target, str) or not target or target in seen_targets:
            continue
        seen_targets.add(target)
        source = str(entry.get("source") or "unknown")
        fstype = str(entry.get("fstype") or "unknown")
        options = str(entry.get("options") or "")
        try:
            size_bytes = max(int(entry.get("size") or 0), 0)
            available_bytes = max(int(entry.get("avail") or 0), 0)
        except (TypeError, ValueError):
            size_bytes = available_bytes = 0
        eligible, reason = storage_mount_eligibility(
            target,
            fstype,
            options,
            available_bytes,
            valid_storage_path=valid_storage_path,
        )
        mounts.append({
            "mount_point": target,
            "source": source,
            "filesystem": fstype,
            "size_bytes": size_bytes,
            "available_bytes": available_bytes,
            "writable": "rw" in {option.strip() for option in options.split(",")},
            "eligible": eligible,
            "unavailable_reason": reason,
        })
    return sorted(mounts, key=lambda mount: mount["mount_point"])
