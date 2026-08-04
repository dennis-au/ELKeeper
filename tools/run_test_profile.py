#!/usr/bin/env python3
"""Run ELKeeper's documented regression profiles with explicit live-test gates.

The full profile includes destructive host rounds. This runner deliberately
requires an operator-supplied command for those rounds rather than silently
reducing the requested coverage to packaging checks.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
from typing import Iterable


@dataclass(frozen=True)
class Check:
    name: str
    command: tuple[str, ...]


def frontend_command(root: Path, *arguments: str) -> tuple[str, ...]:
    """Run Node 22 locally when available, otherwise through Podman."""

    frontend = root / "frontend"
    if shutil.which("node"):
        return ("npm", "--prefix", str(frontend), *arguments)
    return (
        "podman",
        "run",
        "--rm",
        "-v",
        f"{frontend}:/frontend:Z",
        "-w",
        "/frontend",
        "node:22-bookworm-slim",
        "npm",
        *arguments,
    )


def python_command(root: Path, *arguments: str) -> tuple[str, ...]:
    """Use host Python only when it has controller dependencies installed."""

    available = subprocess.run(
        [sys.executable, "-c", "import fastapi"], capture_output=True, check=False
    ).returncode == 0
    if available:
        return (sys.executable, *arguments)
    image = subprocess.run(
        ["podman", "inspect", "--format", "{{.Image}}", "elastic-control-plane"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    return (
        "podman", "run", "--rm", "-v", f"{root}:/opt/elastic-control:Z", "-w", "/opt/elastic-control",
        "--entrypoint", "python", image, *arguments,
    )


def changed_playbooks(root: Path) -> tuple[Path, ...]:
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD", "--", "ansible/playbooks"],
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError:
        return ()
    if result.returncode:
        return ()
    return tuple(root / line for line in result.stdout.splitlines() if line.endswith((".yml", ".yaml")))


def five_minute_checks(root: Path) -> list[Check]:
    checks = [
        Check("Python unit/API suite", python_command(root, "-m", "unittest", "discover", "-s", "tests", "-q")),
        Check("Strict refactor boundaries", (sys.executable, "tools/check_refactor_boundaries.py", "--strict")),
        Check("Strict table ownership", (sys.executable, "tools/check_table_ownership.py", "--strict")),
        Check("Source safety", (sys.executable, "tools/check_source_safety.py")),
        Check("Frontend dependency install", frontend_command(root, "ci", "--no-audit", "--no-fund")),
        Check("Frontend Vitest", frontend_command(root, "test", "--", "--run")),
        Check("Frontend TypeScript", frontend_command(root, "run", "typecheck")),
    ]
    checks.extend(
        Check(f"Ansible syntax: {playbook.name}", ("ansible-playbook", "--syntax-check", str(playbook)))
        for playbook in changed_playbooks(root)
    )
    return checks


def fifteen_minute_checks(root: Path, image_tag: str) -> list[Check]:
    checks = five_minute_checks(root)
    checks.extend(
        [
            Check("Frontend production build", frontend_command(root, "run", "build")),
            *(
                Check(f"Ansible syntax: {playbook.name}", ("ansible-playbook", "--syntax-check", str(playbook)))
                for playbook in sorted((root / "ansible" / "playbooks").glob("*.yml"))
            ),
            Check("Controller image build", ("podman", "build", "-t", image_tag, "-f", "Containerfile", ".")),
            Check(
                "Candidate Python suite",
                ("podman", "run", "--rm", "--entrypoint", "python", image_tag, "-m", "unittest", "discover", "-s", "tests", "-q"),
            ),
            Check("Isolated candidate smoke", (sys.executable, "tools/smoke_candidate.py", "--image", image_tag)),
        ]
    )
    return checks


def run_checks(root: Path, checks: Iterable[Check], dry_run: bool) -> int:
    failed = False
    for check in checks:
        print(f"==> {check.name}")
        print("    " + shlex.join(check.command))
        if dry_run:
            continue
        result = subprocess.run(check.command, cwd=root, check=False)
        if result.returncode:
            print(f"    FAILED (exit {result.returncode}): {check.name}", file=sys.stderr)
            failed = True
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("profile", choices=("5mins", "15mins", "full"))
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--image-tag", default="localhost/elastic-control-plane:profile-candidate")
    parser.add_argument("--live-round-command", help="Required for the destructive full profile")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if args.profile == "full" and not args.live_round_command:
        parser.error("full profile requires --live-round-command; destructive coverage must be explicit")
    checks = five_minute_checks(root) if args.profile == "5mins" else fifteen_minute_checks(root, args.image_tag)
    if run_checks(root, checks, args.dry_run):
        return 1
    if args.profile != "full":
        return 0
    print("==> Destructive live rounds")
    print("    " + args.live_round_command)
    if args.dry_run:
        return 0
    return subprocess.run(args.live_round_command, cwd=root, shell=True, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
