from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.check_source_safety import findings


class SourceSafetyTests(unittest.TestCase):
    def test_flags_lab_address_only_in_scanned_source_roots(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "app").mkdir()
            address = "192" + ".168.0.104"
            (root / "app" / "module.py").write_text(f"address = '{address}'\n", encoding="utf-8")
            self.assertEqual(findings(root), ["app/module.py: lab IPv4 address"])

    def test_ignores_unscanned_documentation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            address = "192" + ".168.0.104"
            (root / "README.md").write_text(f"{address}\n", encoding="utf-8")
            self.assertEqual(findings(root), [])
