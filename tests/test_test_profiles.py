from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from tools import run_test_profile


ROOT = Path(__file__).resolve().parents[1]


class TestProfileRunnerTests(unittest.TestCase):
    def test_five_minute_profile_contains_required_non_destructive_gates(self):
        names = [check.name for check in run_test_profile.five_minute_checks(ROOT)]
        self.assertIn("Python unit/API suite", names)
        self.assertIn("Strict refactor boundaries", names)
        self.assertIn("Strict table ownership", names)
        self.assertIn("Source safety", names)
        self.assertIn("Frontend dependency install", names)
        self.assertIn("Frontend Vitest", names)
        self.assertIn("Frontend TypeScript", names)

    def test_frontend_uses_node_container_when_node_is_unavailable(self):
        with patch.object(run_test_profile.shutil, "which", return_value=None):
            command = run_test_profile.frontend_command(ROOT, "run", "build")
        self.assertEqual(command[:3], ("podman", "run", "--rm"))
        self.assertIn("node:22-bookworm-slim", command)

    def test_frontend_targets_the_frontend_directory_when_node_is_available(self):
        with patch.object(run_test_profile.shutil, "which", return_value="/usr/bin/node"):
            command = run_test_profile.frontend_command(ROOT, "test")
        self.assertEqual(command[:3], ("npm", "--prefix", str(ROOT / "frontend")))

    def test_changed_playbooks_is_empty_when_git_is_unavailable(self):
        with patch.object(run_test_profile.subprocess, "run", side_effect=FileNotFoundError):
            self.assertEqual(run_test_profile.changed_playbooks(ROOT), ())

    def test_full_profile_requires_explicit_destructive_round_command(self):
        with self.assertRaises(SystemExit) as raised:
            run_test_profile.main(["full", "--root", str(ROOT), "--dry-run"])
        self.assertEqual(raised.exception.code, 2)
