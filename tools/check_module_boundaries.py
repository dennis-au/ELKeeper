#!/usr/bin/env python3
"""Report (or strictly reject) private cross-module imports.

Phase 0 deliberately defaults to report-only.  The same command can be used
in CI with ``--strict`` once the extraction reaches Phase 8.
"""

from __future__ import annotations

import argparse
import ast
from pathlib import Path
import sys


PUBLIC_FILES = {
    "__init__",
    "contracts",
    "api",
}

# Feature modules must depend on public contracts, never on the application
# assembly or its legacy console implementation.  The assembly may import
# feature modules; the direction is intentionally one-way.
COMPATIBILITY_MODULES = {"app.main", "app.console"}


def _module_name(path: Path, root: Path) -> str | None:
    try:
        relative = path.relative_to(root).with_suffix("")
    except ValueError:
        return None
    parts = relative.parts
    if len(parts) < 3 or parts[0] != "app" or parts[1] != "modules":
        return None
    return parts[2]


def _import_target(node: ast.ImportFrom | ast.Import) -> list[str]:
    if isinstance(node, ast.ImportFrom):
        return [node.module or ""]
    return [alias.name for alias in node.names]


def find_violations(source_root: Path) -> list[str]:
    """Find imports of another module's private implementation files."""

    app_root = source_root / "app"
    modules_root = app_root / "modules"
    violations: list[str] = []
    if not modules_root.exists():
        return violations

    for path in modules_root.rglob("*.py"):
        source_module = _module_name(path, source_root)
        if source_module is None:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as error:
            violations.append(f"{path}: syntax error: {error}")
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            for target in _import_target(node):
                if target in COMPATIBILITY_MODULES:
                    violations.append(
                        f"{path.relative_to(source_root)}:{node.lineno}: "
                        f"module imports compatibility implementation {target}"
                    )
                    continue
                parts = target.split(".")
                if len(parts) < 4 or parts[:2] != ["app", "modules"]:
                    continue
                target_module = parts[2]
                target_file = parts[3]
                if target_module == source_module or target_file in PUBLIC_FILES:
                    continue
                violations.append(
                    f"{path.relative_to(source_root)}:{node.lineno}: "
                    f"{source_module} imports private {target}"
                )
    return sorted(set(violations))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--strict", action="store_true", help="exit 1 when violations exist")
    args = parser.parse_args(argv)
    violations = find_violations(args.root)
    if violations:
        print("Module boundary report:")
        print("\n".join(f"- {item}" for item in violations))
    else:
        print("Module boundary report: no private cross-module imports found.")
    return 1 if args.strict and violations else 0


if __name__ == "__main__":
    sys.exit(main())
