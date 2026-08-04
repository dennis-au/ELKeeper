#!/usr/bin/env python3
"""Check that direct SQL access stays within the declared table owner.

The checker is intentionally report-only until the final refactor phase.  It
still understands compatibility files so the report can distinguish legacy
access that must be migrated from an actual cross-feature violation.
"""

from __future__ import annotations

import argparse
import ast
from pathlib import Path
import re
import sys

try:
    from app.refactor_ownership import (
        SCHEMA_MIGRATION_FILES,
        TABLE_OWNERSHIP,
        TABLE_READ_ADAPTERS,
        TABLE_READ_ADAPTER_FILES,
    )
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from app.refactor_ownership import (
        SCHEMA_MIGRATION_FILES,
        TABLE_OWNERSHIP,
        TABLE_READ_ADAPTERS,
        TABLE_READ_ADAPTER_FILES,
    )


SQL_TABLE_RE = re.compile(
    r"\b(?:FROM|JOIN|INTO|UPDATE(?!\s+OF\b)|TABLE|REFERENCES)\s+"
    r"(?:IF\s+(?:NOT\s+)?EXISTS\s+)?[\"`\[]?"
    r"([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)

SQL_NON_TABLE_KEYWORDS = {
    "abort",
    "begin",
    "do",
    "end",
    "on",
    "of",
    "raise",
    "select",
    "set",
    "values",
    "where",
    # SQLite's schema catalog is a platform introspection surface, not an
    # application-owned table.
    "sqlite_master",
}

# The remaining facades are import-only compatibility surfaces.  Application
# assembly is no longer exempt: new literal SQL in ``app.main`` must move to an
# owning repository before the strict gate can pass.
COMPATIBILITY_OWNERS = {
    "app.console": "compatibility",
    "app.maintenance_store": "maintenance",
    "app.maintenance_observation": "maintenance",
    "app.maintenance_api": "maintenance",
    "app.maintenance_execution": "maintenance",
}


def _owner_for_path(path: Path, root: Path) -> str | None:
    relative = path.relative_to(root)
    parts = relative.parts
    if len(parts) >= 3 and parts[0] == "app" and parts[1] == "modules":
        return parts[2]
    module = "app." + ".".join(relative.with_suffix("").parts[1:]) if parts and parts[0] == "app" else ""
    return COMPATIBILITY_OWNERS.get(module, "unowned")


def _sql_literals(tree: ast.AST):
    """Yield only literal SQL passed directly to a SQLite execution call."""

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr not in {
            "execute",
            "executemany",
            "executescript",
        }:
            continue
        argument = node.args[0]
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            yield node.lineno, argument.value


def _is_read_only(sql: str) -> bool:
    return bool(re.match(r"\s*(?:SELECT|WITH)\b", sql, re.IGNORECASE))


def _is_schema_definition(sql: str) -> bool:
    return bool(re.match(r"\s*(?:CREATE|ALTER|PRAGMA)\b", sql, re.IGNORECASE))


def find_violations(source_root: Path) -> list[str]:
    """Return direct SQL accesses whose file owner differs from table owner."""

    app_root = source_root / "app"
    violations: list[str] = []
    for path in sorted(app_root.rglob("*.py")):
        if any(part in {"__pycache__", ".git"} for part in path.parts):
            continue
        owner = _owner_for_path(path, source_root)
        if owner is None:
            continue
        relative_path = path.relative_to(source_root).as_posix()
        if relative_path in SCHEMA_MIGRATION_FILES:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as error:
            violations.append(f"{path.relative_to(source_root)}: syntax error: {error}")
            continue
        for line, sql in _sql_literals(tree):
            for table in SQL_TABLE_RE.findall(sql):
                if table.lower() in SQL_NON_TABLE_KEYWORDS:
                    continue
                table_owner = TABLE_OWNERSHIP.get(table)
                if not table_owner:
                    violations.append(
                        f"{path.relative_to(source_root)}:{line}: owner {owner} accesses "
                        f"unregistered table {table}"
                    )
                    continue
                # A compatibility implementation is allowed to touch the
                # tables it is migrating, but still appears in report mode.
                normalized_owner = owner.split(".", 1)[0]
                expected_owner = table_owner.split(".", 1)[0]
                if owner == "compatibility" or normalized_owner == expected_owner:
                    continue
                if (
                    _is_read_only(sql)
                    and table in TABLE_READ_ADAPTERS.get(normalized_owner, set())
                    and relative_path in TABLE_READ_ADAPTER_FILES.get(normalized_owner, frozenset())
                ):
                    continue
                if relative_path == "app/modules/maintenance/store.py" and _is_schema_definition(sql):
                    continue
                violations.append(
                    f"{path.relative_to(source_root)}:{line}: "
                    f"owner {owner} accesses {table} owned by {table_owner}"
                )
    return sorted(set(violations))


def find_route_sql_violations(source_root: Path) -> list[str]:
    """Reject persistence statements in HTTP handlers.

    Routers may open an injected transaction so a multi-step request remains
    atomic, but table reads and writes must be delegated to the owning public
    repository/service contract. This catches the common regression where a
    route gains a one-off ``connection.execute`` despite strict table owner
    matching still succeeding.
    """

    violations: list[str] = []
    for path in sorted((source_root / "app" / "modules").glob("*/http.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as error:
            violations.append(f"{path.relative_to(source_root)}: syntax error: {error}")
            continue
        for line, _sql in _sql_literals(tree):
            violations.append(
                f"{path.relative_to(source_root)}:{line}: route handler executes SQL directly"
            )
    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--strict", action="store_true", help="exit 1 when violations exist")
    args = parser.parse_args(argv)
    violations = [*find_violations(args.root), *find_route_sql_violations(args.root)]
    if violations:
        print("Table ownership report:")
        print("\n".join(f"- {item}" for item in violations))
    else:
        print("Table ownership report: no cross-owner SQL access or route-level SQL found.")
    return 1 if args.strict and violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
