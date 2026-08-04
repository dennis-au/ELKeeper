#!/usr/bin/env python3
"""Prepare release metadata from commits since the latest semantic tag."""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import re
import subprocess


SEMVER = re.compile(r"^v(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)$")


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, check=True, text=True, capture_output=True
    )
    return result.stdout.strip()


def version_tuple(tag: str) -> tuple[int, int, int]:
    match = SEMVER.fullmatch(tag)
    if not match:
        raise ValueError(f"unsupported semantic tag: {tag}")
    return tuple(int(match.group(name)) for name in ("major", "minor", "patch"))


def latest_tag(root: Path) -> str:
    tags = git(root, "tag", "--list", "v[0-9]*.[0-9]*.[0-9]*", "--sort=-version:refname")
    if not tags:
        raise RuntimeError("no semantic release tag exists")
    return tags.splitlines()[0]


def commits_since(root: Path, tag: str) -> list[str]:
    output = git(root, "log", f"{tag}..HEAD", "--format=%s")
    return [line.strip() for line in output.splitlines() if line.strip()]


def next_version(current: tuple[int, int, int], subjects: list[str]) -> tuple[int, int, int]:
    breaking = any(
        "BREAKING CHANGE" in subject
        or re.match(r"^[a-z]+(?:\([^)]*\))?!:", subject, re.IGNORECASE)
        for subject in subjects
    )
    feature = any(re.match(r"^feat(?:\([^)]*\))?:", subject, re.IGNORECASE) for subject in subjects)
    if breaking:
        return current[0] + 1, 0, 0
    if feature:
        return current[0], current[1] + 1, 0
    return current[0], current[1], current[2] + 1


def replace_versions(path: Path, old: str, new: str, count: int) -> None:
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(r'("version"\s*:\s*")' + re.escape(old) + r'(")')
    updated, replacements = pattern.subn(rf"\g<1>{new}\g<2>", text, count=count)
    if replacements != count:
        raise RuntimeError(f"expected {count} version fields in {path}, found {replacements}")
    path.write_text(updated, encoding="utf-8")


def package_version(path: Path) -> str:
    data = json.loads(path.read_text(encoding="utf-8"))
    version = data.get("version")
    if not isinstance(version, str):
        raise RuntimeError(f"missing package version in {path}")
    return version


def changelog_entry(version: str, tag: str, subjects: list[str]) -> str:
    bullets = "\n".join(f"- {subject}" for subject in dict.fromkeys(subjects[:8]))
    return (
        f"## [{version}] - {date.today().isoformat()}\n\n"
        "### Reviewed changes\n\n"
        f"{bullets}\n\n"
        "### Verification\n\n"
        f"- Scheduled review checks passed for changes since `{tag}`.\n"
        "- No live controller deployment or replacement is performed by this workflow.\n"
    )


def update_changelog(path: Path, entry: str) -> None:
    marker = "## ["
    text = path.read_text(encoding="utf-8")
    index = text.find(marker)
    if index < 0:
        raise RuntimeError(f"no release entry found in {path}")
    path.write_text(text[:index] + entry + "\n" + text[index:], encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--base-tag")
    parser.add_argument("--notes-path", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    tag = args.base_tag or latest_tag(root)
    subjects = commits_since(root, tag)
    if not subjects:
        print("NO_RELEASE")
        return 0

    current = version_tuple(tag)
    new_tuple = next_version(current, subjects)
    old_version = ".".join(str(part) for part in current)
    new_version = ".".join(str(part) for part in new_tuple)
    package_path = root / "frontend" / "package.json"
    lock_path = root / "frontend" / "package-lock.json"
    if package_version(package_path) == old_version:
        replace_versions(package_path, old_version, new_version, 1)
        replace_versions(lock_path, old_version, new_version, 2)
        update_changelog(root / "CHANGELOG.md", changelog_entry(new_version, tag, subjects))
    elif package_version(package_path) != new_version or f"## [{new_version}]" not in (root / "CHANGELOG.md").read_text(encoding="utf-8"):
        raise RuntimeError(f"release metadata does not match {tag} or prepared {new_version}")
    args.notes_path.write_text(
        f"# ELKeeper v{new_version}\n\n"
        + changelog_entry(new_version, tag, subjects),
        encoding="utf-8",
    )
    print(new_version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
