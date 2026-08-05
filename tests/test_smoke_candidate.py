from __future__ import annotations

from pathlib import Path
from unittest.mock import patch
import unittest

from tools import smoke_candidate


class CandidateSmokeTests(unittest.TestCase):
    def test_isolated_credentials_are_generated_without_reading_the_live_env_file(self):
        with patch.object(smoke_candidate.secrets, "token_urlsafe", side_effect=("app-key", "admin-password")):
            credentials = smoke_candidate.isolated_credentials()

        self.assertEqual(
            credentials,
            {
                "APP_SECRET_KEY": "app-key",
                "ADMIN_USERNAME": "smoke-operator",
                "ADMIN_PASSWORD": "admin-password",
            },
        )
        source = Path(smoke_candidate.__file__).read_text(encoding="utf-8")
        self.assertNotIn("--env-file", source)
        self.assertNotIn('root / ".env"', source)
