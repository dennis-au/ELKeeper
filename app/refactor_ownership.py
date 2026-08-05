"""Phase-0 persistence ownership registry.

This module is metadata only.  It does not participate in request handling;
tests and tooling use it to prevent new tables from being added without an
explicit owner while the monolith is extracted incrementally.
"""

from __future__ import annotations

import ast
from pathlib import Path
import re
from typing import Any, Dict, Iterable, Mapping


TABLE_OWNERSHIP: Dict[str, str] = {
    "users": "platform.security",
    "controller_settings": "platform.config",
    "nodes": "hosts",
    "controller_ssh_keys": "controller_identity",
    "assignments": "workloads.compatibility",
    "clusters": "clusters",
    "memberships": "clusters",
    "cluster_assignments": "workloads",
    "workload_change_batches": "workloads",
    "workload_observations": "versions",
    "runs": "platform.runs",
    "host_runtime_observations": "observability",
    "cluster_zoning_observations": "clusters",
    "audit_events": "platform.audit",
    "maintenance_policies": "maintenance",
    "maintenance_plans": "maintenance",
    "maintenance_steps": "maintenance",
    "maintenance_checkpoints": "maintenance",
    "host_maintenance_state": "maintenance",
    "maintenance_locks": "maintenance",
    "maintenance_schema_migrations": "platform.db",
    "certificate_trust_domains": "certificates",
    "certificate_policies": "certificates",
    "certificate_assets": "certificates",
    "certificate_generations": "certificates",
    "certificate_observations": "certificates",
    "certificate_trust_consumers": "certificates",
    "certificate_deployments": "certificates",
    "certificate_operations": "certificates",
    "certificate_operation_steps": "certificates",
    "certificate_schema_migrations": "platform.db",
}

# Read-only projections used by maintenance planning are explicit public
# contracts, not permission to mutate another module's tables.  The table
# checker uses this registry to distinguish an approved read adapter from a
# legacy cross-owner write that still needs extraction.
TABLE_READ_ADAPTERS: Dict[str, set[str]] = {
    "maintenance": {
        "nodes",
        "clusters",
        "memberships",
        "cluster_assignments",
        "workload_observations",
        "host_runtime_observations",
        "runs",
    },
}

# A read capability belongs to a narrow projection, not to every source file
# in a module. This prevents a future maintenance writer from gaining broad
# cross-table access merely because maintenance planning has approved reads.
TABLE_READ_ADAPTER_FILES: Dict[str, frozenset[str]] = {
    "maintenance": frozenset({
        "app/modules/maintenance/repository.py",
        "app/modules/maintenance/observation.py",
    }),
}

# Route ownership is a compatibility registry, not an enforcement hook. The
# ordered rules describe the eventual module owner while legacy handlers still
# live in ``main.py`` and ``console.py``.
ROUTE_OWNERSHIP_RULES: tuple[tuple[str | None, str, str], ...] = (
    ("POST", "/api/auth/login", "platform.security"),
    (None, "/api/auth/reveal-grants", "secrets"),
    (None, "/api/assignments", "workloads"),
    (None, "/api/clusters/{cluster_id}/maintenance-policy", "maintenance"),
    (None, "/api/maintenance", "maintenance"),
    (None, "/api/nodes/{node_id}/maintenance", "maintenance"),
    (None, "/api/clusters/{cluster_id}/sensitive-items", "secrets"),
    (None, "/api/clusters/{cluster_id}/certificates", "certificates"),
    (None, "/api/clusters/{cluster_id}/ca-rotation-preview", "certificates"),
    (None, "/api/certificates/{certificate_id}", "certificates"),
    (None, "/api/clusters/{cluster_id}/certificate-policy", "certificates"),
    (None, "/api/clusters/{cluster_id}/certificate-compatibility", "certificates"),
    (None, "/api/clusters/{cluster_id}/certificate-trust-consumers", "certificates"),
    (None, "/api/clusters/{cluster_id}/certificate-operations", "certificates"),
    (None, "/api/certificate-operations/{operation_id}", "certificates"),
    (None, "/api/clusters/{cluster_id}/settings", "clusters"),
    (None, "/api/clusters/{cluster_id}/versions", "versions"),
    (None, "/api/clusters/{cluster_id}/workload-changes", "workloads"),
    (None, "/api/clusters/{cluster_id}/topology", "workloads"),
    (None, "/api/clusters/{cluster_id}/log-monitoring", "log_monitoring"),
    (None, "/api/clusters/{cluster_id}/zoning", "clusters"),
    (None, "/api/clusters/{cluster_id}/provider", "clusters"),
    (None, "/api/clusters/{cluster_id}/members", "clusters"),
    (None, "/api/clusters/{cluster_id}/assignments", "workloads"),
    (None, "/api/clusters", "clusters"),
    (None, "/api/controller", "controller_identity"),
    (None, "/api/dashboard", "observability"),
    (None, "/api/health", "platform.app"),
    (None, "/api/hosts", "hosts"),
    (None, "/api/nodes/{node_id}/controller-key", "controller_identity"),
    (None, "/api/nodes/{node_id}/runtime", "observability"),
    (None, "/api/nodes/{node_id}/storage", "hosts"),
    (None, "/api/nodes/{node_id}/reboot", "maintenance"),
    (None, "/api/nodes/{node_id}/roles", "workloads.compatibility"),
    (None, "/api/nodes", "hosts"),
    (None, "/api/runs", "platform.runs"),
    ("GET", "/", "platform.app"),
    ("GET", "/{frontend_path:path}", "platform.app"),
)

_CREATE_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"[\"`\[]?([A-Za-z_][A-Za-z0-9_]*)[\"`\]]?",
    re.IGNORECASE,
)

# SQL is inspected only when it is passed directly to a SQLite execution
# method. This avoids treating prose or unrelated strings as persistence
# accesses while still covering the normal ``connection.execute`` patterns.
_SQL_CALL_NAMES = {"execute", "executemany", "executescript"}
_TABLE_REFERENCE_RE = re.compile(
    r"\b(?:FROM|JOIN|INTO|UPDATE|TABLE|REFERENCES)\s+"
    r"(?:IF\s+(?:NOT\s+)?EXISTS\s+)?"
    r"[\"`\[]?([A-Za-z_][A-Za-z0-9_]*)[\"`\]]?",
    re.IGNORECASE,
)

# These files are the compatibility implementation during extraction. They
# remain reportable in route/import checks, but their direct SQL is explicitly
# allowed until the owning repository migration is complete.
COMPATIBILITY_SQL_FILES = frozenset(
    {
        "app/main.py",
        "app/console.py",
        "app/maintenance_store.py",
    }
)

# Platform schema code is the sole explicit cross-owner writer. It runs only
# during controller startup before HTTP routes or background workers begin.
SCHEMA_MIGRATION_FILES = frozenset(
    {
        "app/modules/platform/bootstrap.py",
        "app/modules/platform/migrations.py",
    }
)


def _compatibility_sql_path(path: Path, source_root: Path) -> bool:
    relative = path.relative_to(source_root).as_posix()
    return relative in COMPATIBILITY_SQL_FILES or relative.startswith("app/maintenance")


def _module_owner(path: Path, source_root: Path) -> str | None:
    """Resolve the planned owner represented by a module source path."""

    try:
        relative = path.relative_to(source_root)
    except ValueError:
        return None
    parts = relative.parts
    if len(parts) < 3 or parts[0] != "app" or parts[1] != "modules":
        return None
    module = parts[2]
    if module == "platform" and len(parts) >= 4:
        stem = Path(parts[3]).stem
        if stem not in {"__init__", "contracts"}:
            return f"platform.{stem}"
    return module


def _owner_matches(module_owner: str | None, table_owner: str) -> bool:
    if module_owner is None:
        return False
    return table_owner == module_owner or table_owner.startswith(module_owner + ".")


def discover_sql_references(source_root: Path) -> list[dict[str, Any]]:
    """Return direct SQLite SQL table references with source locations.

    The checker intentionally reports only literal SQL passed as the first
    argument to ``execute``, ``executemany`` or ``executescript``. Dynamic SQL
    remains a follow-up migration concern rather than being guessed here.
    """

    references: list[dict[str, Any]] = []
    scan_root = source_root / "app" if (source_root / "app").is_dir() else source_root
    for path in scan_root.rglob("*.py"):
        if any(part in {".git", "__pycache__"} for part in path.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            function = node.func
            if not isinstance(function, ast.Attribute) or function.attr not in _SQL_CALL_NAMES:
                continue
            argument = node.args[0]
            if not isinstance(argument, ast.Constant) or not isinstance(argument.value, str):
                continue
            for match in _TABLE_REFERENCE_RE.finditer(argument.value):
                references.append(
                    {
                        "path": path.relative_to(source_root).as_posix(),
                        "line": node.lineno,
                        "table": match.group(1).lower(),
                        "owner": _module_owner(path, source_root),
                        "compatibility": _compatibility_sql_path(path, source_root),
                    }
                )
    return references


def sql_ownership_violations(source_root: Path) -> list[dict[str, Any]]:
    """Return SQL references that cross the declared table boundary."""

    violations: list[dict[str, Any]] = []
    for reference in discover_sql_references(source_root):
        if reference["compatibility"]:
            continue
        if reference["path"] in SCHEMA_MIGRATION_FILES:
            continue
        table = str(reference["table"])
        try:
            table_owner = ownership_for(table)
        except KeyError:
            violations.append({**reference, "reason": "unregistered table"})
            continue
        if not _owner_matches(reference["owner"], table_owner):
            violations.append(
                {
                    **reference,
                    "declared_owner": table_owner,
                    "reason": "table accessed outside owning module",
                }
            )
    return violations


# Alias used by CI and future callers that prefer the boundary-checking name.
find_sql_violations = sql_ownership_violations


def discover_tables(source_root: Path) -> set[str]:
    """Return table names declared by application Python sources."""

    tables: set[str] = set()
    for path in source_root.rglob("*.py"):
        if any(part in {".git", "__pycache__"} for part in path.parts):
            continue
        source = path.read_text(encoding="utf-8")
        # Migration registries intentionally construct the table name at
        # runtime.  Do not let the placeholder make ``IF`` look like a real
        # application table in the static inventory.
        if "{self.table_name}" in source:
            source = source.replace("CREATE TABLE IF NOT EXISTS {self.table_name}", "")
        tables.update(_CREATE_TABLE_RE.findall(source))
    return tables


def unowned_tables(source_root: Path) -> set[str]:
    """Return declared tables missing from the ownership registry."""

    return discover_tables(source_root) - set(TABLE_OWNERSHIP)


def ownership_for(table: str) -> str:
    """Return the declared owner or raise a useful error for new tables."""

    try:
        return TABLE_OWNERSHIP[table]
    except KeyError as error:
        raise KeyError(f"No module owner registered for table {table!r}") from error


def route_owner(method: str, path: str) -> str | None:
    """Return the planned owner for a route, or ``None`` when unregistered."""

    normalized_method = method.upper()
    for rule_method, prefix, owner in ROUTE_OWNERSHIP_RULES:
        if rule_method is not None and rule_method != normalized_method:
            continue
        if path == prefix or path.startswith(prefix + "/"):
            return owner
    return None


def unowned_routes(routes: Iterable[Mapping[str, object]]) -> list[dict[str, object]]:
    """Return route records with no planned owner in report-only mode."""

    missing = []
    for route in routes:
        method = str(route.get("method", ""))
        path = str(route.get("path", ""))
        if route_owner(method, path) is None:
            missing.append(dict(route))
    return missing
