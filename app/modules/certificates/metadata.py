"""Safe, value-free metadata extraction for managed certificates."""

from __future__ import annotations

from cryptography import x509
from cryptography.hazmat.primitives import hashes


def certificate_public_metadata(content: bytes) -> dict[str, str]:
    """Return allowlisted certificate metadata without returning key material."""

    certificate = x509.load_pem_x509_certificate(content)
    return {
        "fingerprint": certificate.fingerprint(hashes.SHA256()).hex(":").upper(),
        "expires_at": certificate.not_valid_after_utc.isoformat().replace("+00:00", "Z"),
    }
