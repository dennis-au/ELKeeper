#!/usr/bin/env python3
"""Emit a stable inventory of FastAPI routes for compatibility review."""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import sys

try:
    from app.refactor_ownership import route_owner, unowned_routes
except ModuleNotFoundError:  # pragma: no cover - supports direct script execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.refactor_ownership import route_owner, unowned_routes


HTTP_METHODS = {"get", "post", "put", "patch", "delete", "options", "head"}


def inventory(source_root: Path) -> list[dict[str, object]]:
    routes: list[dict[str, object]] = []
    for path in sorted((source_root / "app").rglob("*.py")):
        if any(part in {"__pycache__", ".git"} for part in path.parts):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                    continue
                if decorator.func.attr not in HTTP_METHODS or not decorator.args:
                    continue
                route = decorator.args[0]
                if not isinstance(route, ast.Constant) or not isinstance(route.value, str):
                    continue
                method = decorator.func.attr.upper()
                routes.append(
                    {
                        "file": str(path.relative_to(source_root)),
                        "method": method,
                        "path": route.value,
                        "handler": node.name,
                        "owner": route_owner(method, route.value),
                    }
                )
    return sorted(routes, key=lambda item: (str(item["path"]), str(item["method"]), str(item["file"])))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--check-ownership",
        action="store_true",
        help="Report unregistered routes and return a non-zero status.",
    )
    args = parser.parse_args()
    routes = inventory(args.root)
    payload = json.dumps(routes, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    else:
        print(payload, end="")
    if args.check_ownership:
        missing = unowned_routes(routes)
        if missing:
            for route in missing:
                print(
                    f"unowned route: {route['method']} {route['path']} ({route['file']})",
                    file=sys.stderr,
                )
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
