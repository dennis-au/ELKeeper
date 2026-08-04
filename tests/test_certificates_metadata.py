from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.modules.certificates import certificate_public_metadata


class CertificateMetadataTests(unittest.TestCase):
    def test_public_metadata_is_allowlisted_and_redacts_certificate_contents(self):
        certificate = SimpleNamespace(
            fingerprint=lambda algorithm: b"fingerprint",
            not_valid_after_utc=datetime(2026, 8, 4, 12, tzinfo=timezone.utc),
        )
        with patch("app.modules.certificates.metadata.x509.load_pem_x509_certificate", return_value=certificate):
            metadata = certificate_public_metadata(b"certificate-bytes")
        self.assertEqual(metadata, {
            "fingerprint": "66:69:6E:67:65:72:70:72:69:6E:74",
            "expires_at": "2026-08-04T12:00:00Z",
        })
        self.assertNotIn("certificate", metadata)
