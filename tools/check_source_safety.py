#!/usr/bin/env python3
"""Reject lab-specific networking and insecure Podman TCP endpoints in source."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys


PATTERNS = {
    "lab IPv4 address": re.compile(r"\b192\.168\.\d{1,3}\.\d{1,3}\b"),
    "insecure Podman TCP endpoint": re.compile(r"podman[^\n]{0,80}tcp://", re.IGNORECASE),
}
SOURCE_ROOTS = ("app", "frontend/src", "ansible", "tests")


def findings(root: Path) -> list[str]:
    result: list[str] = []
    for relative in SOURCE_ROOTS:
        directory = root / relative
        if not directory.exists():
            continue
        for path in directory.rglob("*"):
            if not path.is_file() or path.suffix not in {".py", ".ts", ".tsx", ".yml", ".yaml", ".json"}:
                continue
            text = path.read_text(encoding="utf-8")
            for label, pattern in PATTERNS.items():
                if pattern.search(text):
                    result.append(f"{path.relative_to(root)}: {label}")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    problems = findings(args.root.resolve())
    if problems:
        print("Source safety violations:", *problems, sep="\n", file=sys.stderr)
        return 1
    print("Source safety check: no lab addresses or Podman TCP endpoints found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
