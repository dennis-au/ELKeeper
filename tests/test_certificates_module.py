from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from app.modules.certificates import ca_ssl_context, cluster_ca_path, invalidate_cluster_ca


class CertificateRuntimeTests(unittest.TestCase):
    def test_ca_context_uses_verified_context_and_relaxes_only_legacy_strict_flag(self):
        context = SimpleNamespace(verify_flags=8 | 4)
        ssl_module = SimpleNamespace(
            VERIFY_X509_STRICT=8,
            create_default_context=lambda **kwargs: (self.assertEqual(kwargs, {"cafile": "/tmp/ca.crt"}) or context),
        )

        self.assertIs(ca_ssl_context("/tmp/ca.crt", ssl_module=ssl_module), context)
        self.assertEqual(context.verify_flags, 4)

    def test_cluster_ca_cache_path_and_invalidation_are_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            expected = Path(directory) / "cluster-12.crt"
            self.assertEqual(cluster_ca_path(directory, 12), expected)
            expected.write_text("ca")
            invalidate_cluster_ca(directory, 12)
            self.assertFalse(expected.exists())
            # Cleanup is intentionally idempotent.
            invalidate_cluster_ca(directory, 12)
