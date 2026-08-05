from __future__ import annotations

import json
import ast
from pathlib import Path
import subprocess
import sys
import unittest

from app.refactor_ownership import (
    TABLE_OWNERSHIP,
    discover_tables,
    route_owner,
    unowned_routes,
    unowned_tables,
)
from tools.golden_dtos import GOLDEN_DTO_CONTRACTS, load_all_fixtures, redact_payload, stable_json
from tools.route_inventory import inventory


ROOT = Path(__file__).resolve().parents[1]


class RefactorContractTests(unittest.TestCase):
    def test_maintenance_recovery_contract_is_module_owned_with_compatibility_alias(self):
        from app.maintenance_recovery import classify_recovery as legacy_classify
        from app.modules.maintenance.recovery import classify_recovery

        self.assertIs(legacy_classify, classify_recovery)

    def test_application_assembly_has_no_literal_sql_access(self):
        source = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        statements = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            if not isinstance(node.func, ast.Attribute) or node.func.attr not in {"execute", "executemany", "executescript"}:
                continue
            argument = node.args[0]
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                statements.append((node.lineno, argument.value))
        self.assertEqual(statements, [])

    def test_application_assembly_uses_the_public_version_operations_contract(self):
        source = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn("VersionOperations", source)
        self.assertNotIn("VersionRuntimeService(", source)
        self.assertNotIn("VersionUpgradeService(", source)
        self.assertNotIn("VersionUpgradeWorker(", source)
        self.assertNotIn("VersionUpgradeLauncher(", source)

    def test_application_assembly_uses_the_public_workload_operations_contract(self):
        source = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn("WorkloadOperations", source)
        self.assertNotIn("WorkloadChangeWorker(", source)

    def test_application_assembly_uses_public_workload_change_validation(self):
        source = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn("WorkloadChangeValidator", source)
        self.assertIn("return workload_change_validator().validate", source)

    def test_application_assembly_uses_public_membership_operations(self):
        source = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn("MembershipOperations", source)
        self.assertIn("return membership_operations.require_ready", source)

    def test_application_assembly_uses_the_public_zoning_operations_contract(self):
        source = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn("ZoningOperations", source)
        self.assertNotIn("ZoningService(", source)

    def test_application_assembly_uses_the_public_controller_identity_contract(self):
        source = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn("ControllerIdentityOperations", source)
        self.assertNotIn("ControllerIdentityService(", source)

    def test_application_assembly_uses_the_public_host_operations_contract(self):
        source = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn("HostOperations", source)
        self.assertNotIn("return HostEnrollmentOrchestrator(", source)

    def test_application_assembly_uses_the_public_host_lifecycle_contract(self):
        source = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn("HostLifecycleOperations", source)
        self.assertNotIn("def require_host_no_conflict", source)
        self.assertNotIn("def host_has_assignments", source)
        self.assertNotIn("def launch_host_action", source)

    def test_application_assembly_exposes_but_does_not_register_phase2_reboot_composition(self):
        source = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn("Phase2RebootAdapterFactory", source)
        self.assertIn("def phase2_reboot_adapter_factory", source)
        self.assertNotIn("MAINTENANCE_ADAPTERS", source)

    def test_observability_collector_uses_narrow_dependencies_not_application_core(self):
        source = (ROOT / "app" / "modules" / "observability" / "collector.py").read_text(encoding="utf-8")
        self.assertNotIn("_deps.core", source)
        self.assertNotIn("    core: Any", source)
        for dependency in ("db_factory", "workload_name", "image_version", "open_config", "seal_config", "cluster_record"):
            self.assertIn(dependency, source)

    def test_console_runtime_is_configured_from_explicit_dependencies(self):
        source = (ROOT / "app" / "console_runtime.py").read_text(encoding="utf-8")
        self.assertNotIn("from app import main", source)
        self.assertIn("class ConsoleRuntimeDependencies", source)
        self.assertIn("def configure_runtime", source)
        assembly = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn("console.configure_runtime(", assembly)

    def test_every_declared_table_has_one_owner(self):
        self.assertEqual(unowned_tables(ROOT / "app"), set())
        self.assertEqual(len(TABLE_OWNERSHIP), len(set(TABLE_OWNERSHIP)))

    def test_route_inventory_contains_compatibility_surface(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "route_inventory.py"), "--root", str(ROOT)],
            check=True,
            capture_output=True,
            text=True,
        )
        routes = json.loads(result.stdout)
        keys = {(item["method"], item["path"]) for item in routes}
        self.assertIn(("GET", "/api/health"), keys)
        self.assertIn(("GET", "/api/clusters"), keys)
        self.assertIn(("GET", "/api/runs/{run_id}/events"), keys)

    def test_route_inventory_reports_an_owner_for_every_current_route(self):
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "tools" / "route_inventory.py"),
                "--root",
                str(ROOT),
                "--check-ownership",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        routes = json.loads(result.stdout)
        self.assertEqual(unowned_routes(routes), [])
        self.assertTrue(all(route["owner"] for route in routes))

    def test_route_surface_matches_the_checked_in_compatibility_snapshot(self):
        fixture = ROOT / "tests" / "fixtures" / "route_inventory.json"
        self.assertTrue(fixture.is_file(), "regenerate the fixture deliberately when changing public routes")
        expected = json.loads(fixture.read_text(encoding="utf-8"))
        self.assertEqual(inventory(ROOT), expected)

    def test_route_owner_leaves_new_route_unregistered(self):
        self.assertIsNone(route_owner("GET", "/api/new-feature/endpoint"))

    def test_certificate_ca_rotation_preview_is_owned_by_certificates(self):
        self.assertEqual(
            route_owner("POST", "/api/clusters/{cluster_id}/ca-rotation-preview"),
            "certificates",
        )

    def test_golden_dto_fixtures_are_complete_and_non_secret(self):
        fixtures = load_all_fixtures(ROOT / "tests" / "fixtures" / "golden")
        self.assertEqual(set(fixtures), set(GOLDEN_DTO_CONTRACTS))
        for name, fixture in fixtures.items():
            self.assertIn("route", fixture, name)
            self.assertIn("response", fixture, name)
            self.assertEqual(stable_json(fixture), stable_json(fixture))

    def test_golden_redaction_is_recursive_and_deterministic(self):
        payload = {
            "token": "do-not-store",
            "nested": {"password": "do-not-store", "value": 2},
        }
        self.assertEqual(
            redact_payload(payload),
            {
                "token": "[REDACTED]",
                "nested": {"password": "[REDACTED]", "value": 2},
            },
        )

    def test_boundary_checker_is_report_only_by_default(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "tools" / "check_module_boundaries.py"), "--root", str(ROOT)],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("Module boundary report", result.stdout)

    def test_boundary_checker_passes_in_strict_mode_for_current_modules(self):
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "tools" / "check_module_boundaries.py"),
                "--root",
                str(ROOT),
                "--strict",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("no private cross-module imports", result.stdout)

    def test_aggregate_boundary_checker_passes_in_strict_mode(self):
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "tools" / "check_refactor_boundaries.py"),
                "--root",
                str(ROOT),
                "--strict",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("no private cross-module imports", result.stdout)
        self.assertIn("no private cross-feature imports", result.stdout)
        self.assertIn("no cross-owner SQL access", result.stdout)

    def test_frontend_boundary_checker_rejects_route_page_implementation(self):
        from tools.check_frontend_boundaries import find_route_page_violations

        with __import__("tempfile").TemporaryDirectory() as temporary:
            root = Path(temporary)
            page = root / "src" / "pages" / "RolesPage.tsx"
            page.parent.mkdir(parents=True)
            page.write_text(
                "import { useState } from 'react';\nexport function RolesPage() { return useState(0); }\n",
                encoding="utf-8",
            )
            violations = find_route_page_violations(root)
        self.assertTrue(any("route-page implementation" in item for item in violations))

    def test_inventory_discovers_maintenance_tables(self):
        tables = discover_tables(ROOT / "app")
        self.assertIn("maintenance_plans", tables)
        self.assertEqual(TABLE_OWNERSHIP["maintenance_plans"], "maintenance")

    def test_maintenance_router_uses_public_platform_contracts(self):
        tree = ast.parse((ROOT / "app" / "maintenance_api.py").read_text(encoding="utf-8"))
        imports = [node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))]
        imported_modules = {
            node.module if isinstance(node, ast.ImportFrom) else alias.name
            for node in imports
            for alias in (node.names if isinstance(node, ast.Import) else [ast.alias(name=node.module or "")])
        }
        self.assertNotIn("app.main", imported_modules)
        self.assertTrue(any(module == "app.modules.platform" for module in imported_modules))

    def test_boundary_checker_flags_assembly_imports_from_feature_modules(self):
        from tools.check_module_boundaries import find_violations

        with __import__("tempfile").TemporaryDirectory() as temporary:
            root = Path(temporary)
            module = root / "app" / "modules" / "example"
            module.mkdir(parents=True)
            (module / "__init__.py").write_text("import app.main\n", encoding="utf-8")
            violations = find_violations(root)
        self.assertTrue(any("compatibility implementation app.main" in item for item in violations))

    def test_boundary_checker_flags_private_cross_module_repository_imports(self):
        from tools.check_module_boundaries import find_violations

        with __import__("tempfile").TemporaryDirectory() as temporary:
            root = Path(temporary)
            maintenance = root / "app" / "modules" / "maintenance"
            workloads = root / "app" / "modules" / "workloads"
            maintenance.mkdir(parents=True)
            workloads.mkdir(parents=True)
            (maintenance / "store.py").write_text(
                "from app.modules.workloads.repository import WorkloadRepository\n",
                encoding="utf-8",
            )
            (workloads / "repository.py").write_text("", encoding="utf-8")
            violations = find_violations(root)
        self.assertTrue(any("maintenance imports private app.modules.workloads.repository" in item for item in violations))

    def test_table_ownership_checker_reports_legacy_cross_module_sql(self):
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "tools" / "check_table_ownership.py"),
                "--root",
                str(ROOT),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("Table ownership report", result.stdout)
        self.assertIn("Table ownership report", result.stdout)
        self.assertNotIn("unregistered table sqlite_master", result.stdout)

    def test_table_ownership_checker_allows_declared_read_adapter(self):
        from tools.check_table_ownership import find_violations

        violations = find_violations(ROOT)
        self.assertFalse(any("maintenance_observation.py:127" in item for item in violations))

    def test_table_ownership_checker_requires_a_declared_read_adapter_file(self):
        from tools.check_table_ownership import find_violations

        with __import__("tempfile").TemporaryDirectory() as temporary:
            root = Path(temporary)
            module = root / "app" / "modules" / "maintenance"
            module.mkdir(parents=True)
            (root / "app" / "refactor_ownership.py").write_text(
                "TABLE_OWNERSHIP = {'nodes': 'hosts'}\n"
                "TABLE_READ_ADAPTERS = {'maintenance': {'nodes'}}\n"
                "TABLE_READ_ADAPTER_FILES = {'maintenance': frozenset({'app/modules/maintenance/repository.py'})}\n"
                "SCHEMA_MIGRATION_FILES = frozenset()\n",
                encoding="utf-8",
            )
            (module / "service.py").write_text(
                "def read(connection):\n    return connection.execute('SELECT * FROM nodes')\n",
                encoding="utf-8",
            )
            violations = find_violations(root)
        self.assertTrue(any("owner maintenance accesses nodes" in item for item in violations))

    def test_table_ownership_checker_strictly_rejects_cross_module_sql(self):
        from tools.check_table_ownership import find_violations

        with __import__("tempfile").TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = root / "app" / "modules"
            (app / "hosts").mkdir(parents=True)
            (app / "clusters").mkdir(parents=True)
            (root / "app" / "refactor_ownership.py").write_text(
                "TABLE_OWNERSHIP = {'clusters': 'clusters'}\n", encoding="utf-8"
            )
            (app / "hosts" / "repository.py").write_text(
                "def read(connection):\n    return connection.execute('SELECT * FROM clusters')\n",
                encoding="utf-8",
            )
            # Importing the real registry is not needed for the pure helper;
            # exercise the production checker through its output contract.
            violations = find_violations(root)
        self.assertTrue(any("owner hosts accesses clusters" in item for item in violations))

    def test_table_ownership_checker_rejects_direct_route_sql(self):
        from tools.check_table_ownership import find_route_sql_violations

        with __import__("tempfile").TemporaryDirectory() as temporary:
            root = Path(temporary)
            route = root / "app" / "modules" / "clusters" / "http.py"
            route.parent.mkdir(parents=True)
            route.write_text(
                "def route(connection):\n    return connection.execute('SELECT * FROM clusters')\n",
                encoding="utf-8",
            )
            violations = find_route_sql_violations(root)
        self.assertTrue(any("route handler executes SQL directly" in item for item in violations))

    def test_table_ownership_checker_only_scans_sql_calls_and_reports_unknown_tables(self):
        from tools.check_table_ownership import find_violations

        with __import__("tempfile").TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "app" / "modules" / "hosts"
            repository.mkdir(parents=True)
            (repository / "repository.py").write_text(
                '"""SELECT * FROM clusters should not be treated as SQL."""\n'
                "def read(connection):\n"
                "    return connection.execute('SELECT * FROM future_table')\n",
                encoding="utf-8",
            )
            violations = find_violations(root)
        self.assertTrue(any("unregistered table future_table" in item for item in violations))
        self.assertFalse(any("unregistered table clusters" in item for item in violations))

    def test_table_ownership_checker_handles_trigger_and_conflict_keywords(self):
        from tools.check_table_ownership import find_violations

        with __import__("tempfile").TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "app" / "modules" / "clusters"
            repository.mkdir(parents=True)
            (repository / "repository.py").write_text(
                "def write(connection):\n"
                "    connection.execute('UPDATE clusters SET name=? ON CONFLICT(id) DO UPDATE SET name=?')\n"
                "    connection.execute('CREATE TRIGGER x BEFORE UPDATE OF name ON clusters BEGIN SELECT 1; END')\n",
                encoding="utf-8",
            )
            violations = find_violations(root)
        self.assertFalse(any("unregistered table SET" in item for item in violations))
        self.assertFalse(any("unregistered table name" in item for item in violations))
