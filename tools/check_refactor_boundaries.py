#!/usr/bin/env python3
"""Run the route, backend, frontend, and table ownership gates together."""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args(argv)
    commands = [
        [sys.executable, str(args.root / "tools" / "route_inventory.py"), "--root", str(args.root), "--check-ownership"],
        [sys.executable, str(args.root / "tools" / "check_module_boundaries.py"), "--root", str(args.root)],
        [sys.executable, str(args.root / "tools" / "check_frontend_boundaries.py"), "--root", str(args.root / "frontend")],
        [sys.executable, str(args.root / "tools" / "check_table_ownership.py"), "--root", str(args.root)],
    ]
    if args.strict:
        for command in commands[1:]:
            command.append("--strict")
    failed = False
    for command in commands:
        result = subprocess.run(command, cwd=args.root, check=False)
        failed = failed or result.returncode != 0
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
