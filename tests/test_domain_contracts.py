from datetime import datetime, timedelta, timezone
import unittest

from app.modules.certificates import CertificateMetadata, renewal_due
from app.modules.observability import BoundedHistory, StreamToken
from app.modules.secrets import RevealGrant, SecretMetadata
from app.modules.versions import UpgradeGuard, stable_targets


class DomainContractTests(unittest.TestCase):
    def test_secret_and_certificate_metadata_are_public_without_values(self):
        metadata = SecretMetadata("kibana", "service", "Kibana system")
        self.assertNotIn("value", metadata.public())
        cert = CertificateMetadata("cert-1", "kibana", "CN=kibana", ("192.0.2.1",), "2026-01-01T00:00:00Z", "2026-08-10T00:00:00Z", "sha256:x", "/etc/ecp/cert.pem")
        self.assertEqual(cert.public()["san_addresses"], ["192.0.2.1"])
        self.assertTrue(renewal_due("2026-08-10T00:00:00Z", now=datetime(2026, 8, 1, tzinfo=timezone.utc), renew_before_days=30))

    def test_reveal_grant_expiry_and_scope(self):
        grant = RevealGrant("opaque", 7, datetime.now(timezone.utc) + timedelta(minutes=1), "copy")
        self.assertTrue(grant.valid_for(7, "copy"))
        self.assertFalse(grant.valid_for(8, "copy"))

    def test_version_guard_and_bounded_observability(self):
        self.assertFalse(UpgradeGuard.validate("8.12.0", "10.0.0", healthy=True, snapshot_recent=True, master_eligible=3)[0])
        self.assertEqual([target.value for target in stable_targets(["8.10.0", "8.11.0"])], ["8.11.0", "8.10.0"])
        history = BoundedHistory[int](2)
        history.append(1); history.append(2); history.append(3)
        self.assertEqual(history.snapshot(), (2, 3))
        self.assertTrue(StreamToken("x", 10).valid(9))
