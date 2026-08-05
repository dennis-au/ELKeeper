"""Safe, value-free metadata extraction for managed certificates."""

from __future__ import annotations

from datetime import datetime, timezone
import ipaddress
from typing import Iterable

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.x509.oid import ExtendedKeyUsageOID


_EKU_NAMES = {
    ExtendedKeyUsageOID.SERVER_AUTH: "serverAuth",
    ExtendedKeyUsageOID.CLIENT_AUTH: "clientAuth",
    ExtendedKeyUsageOID.CODE_SIGNING: "codeSigning",
    ExtendedKeyUsageOID.EMAIL_PROTECTION: "emailProtection",
    ExtendedKeyUsageOID.TIME_STAMPING: "timeStamping",
    ExtendedKeyUsageOID.OCSP_SIGNING: "ocspSigning",
}


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _fingerprint(certificate: x509.Certificate) -> str:
    return certificate.fingerprint(hashes.SHA256()).hex(":").upper()


def _subject_alternative_names(certificate: x509.Certificate) -> tuple[list[str], list[str]]:
    try:
        names = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return [], []
    return (
        sorted({str(value).lower() for value in names.get_values_for_type(x509.DNSName)}),
        sorted({str(value) for value in names.get_values_for_type(x509.IPAddress)}),
    )


def _extended_key_usage(certificate: x509.Certificate) -> set[object] | None:
    try:
        return set(certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value)
    except x509.ExtensionNotFound:
        return None


def _basic_constraints(certificate: x509.Certificate) -> bool:
    try:
        return bool(certificate.extensions.get_extension_for_class(x509.BasicConstraints).value.ca)
    except x509.ExtensionNotFound:
        return False


def _chain_status(leaf: x509.Certificate, issuers: Iterable[x509.Certificate]) -> str:
    chain = tuple(issuers)
    if not chain:
        if leaf.subject != leaf.issuer:
            return "missing"
        try:
            leaf.verify_directly_issued_by(leaf)
        except (ValueError, TypeError):
            return "invalid"
        return "verified"
    current = leaf
    for issuer in chain:
        try:
            current.verify_directly_issued_by(issuer)
        except (ValueError, TypeError):
            return "invalid"
        current = issuer
    if current.subject != current.issuer:
        return "partial"
    try:
        current.verify_directly_issued_by(current)
    except (ValueError, TypeError):
        return "invalid"
    return "verified"


def _validity_status(certificate: x509.Certificate, now: datetime) -> str:
    current = now.astimezone(timezone.utc)
    if certificate.not_valid_before_utc > current:
        return "not_yet_valid"
    if certificate.not_valid_after_utc < current:
        return "expired"
    return "valid"


def _san_status(
    dns_names: Iterable[str],
    ip_addresses: Iterable[str],
    *,
    expected_dns: Iterable[str],
    expected_ips: Iterable[str],
) -> str:
    expected_dns_values = {str(value).lower() for value in expected_dns if str(value).strip()}
    expected_ip_values = {
        str(ipaddress.ip_address(str(value))) for value in expected_ips if str(value).strip()
    }
    if not expected_dns_values and not expected_ip_values:
        return "not_checked"
    if expected_dns_values.issubset(set(dns_names)) and expected_ip_values.issubset(set(ip_addresses)):
        return "matched"
    return "mismatch"


def _eku_status(certificate: x509.Certificate, purpose: str) -> tuple[str, list[str]]:
    usages = _extended_key_usage(certificate)
    if usages is None:
        return "not_present_allowed", []
    values = sorted(_EKU_NAMES.get(value, value.dotted_string) for value in usages)
    if purpose != "elasticsearch_transport":
        return "valid", values
    required = {ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH}
    return ("valid" if required.issubset(usages) else "missing_required"), values


def inspect_certificate_chain(
    certificate_pem: bytes,
    chain_pems: Iterable[bytes] = (),
    *,
    purpose: str,
    expected_dns: Iterable[str] = (),
    expected_ips: Iterable[str] = (),
    now: datetime | None = None,
) -> dict[str, object]:
    """Inspect PEM material into public evidence without retaining PEM or keys.

    Transport certificates are subject to ELKeeper SAN policy even when the
    legacy Elasticsearch transport configuration uses certificate verification.
    A present transport EKU must authorize both server and client usage; an
    absent EKU remains valid for Elastic's supported compatibility case.
    """

    certificate = x509.load_pem_x509_certificate(certificate_pem)
    issuers = tuple(x509.load_pem_x509_certificate(value) for value in chain_pems)
    dns_names, ip_addresses = _subject_alternative_names(certificate)
    current = now or datetime.now(timezone.utc)
    validity = _validity_status(certificate, current)
    san = _san_status(
        dns_names,
        ip_addresses,
        expected_dns=expected_dns,
        expected_ips=expected_ips,
    )
    eku, usages = _eku_status(certificate, purpose)
    chain = _chain_status(certificate, issuers)
    healthy = (
        validity == "valid"
        and san in {"matched", "not_checked"}
        and eku in {"valid", "not_present_allowed"}
        and chain == "verified"
    )
    return {
        "metadata": {
            "fingerprint": _fingerprint(certificate),
            "subject": certificate.subject.rfc4514_string(),
            "issuer": certificate.issuer.rfc4514_string(),
            "serial_number": format(certificate.serial_number, "X"),
            "not_before": _timestamp(certificate.not_valid_before_utc),
            "not_after": _timestamp(certificate.not_valid_after_utc),
            "san_dns": dns_names,
            "san_ip": ip_addresses,
            "extended_key_usage": usages,
            "is_ca": _basic_constraints(certificate),
        },
        "chain_fingerprints": [_fingerprint(item) for item in issuers],
        "validation": {
            "chain": chain,
            "eku": eku,
            "health": "healthy" if healthy else "degraded",
            "san": san,
            "validity": validity,
        },
    }


def certificate_public_metadata(content: bytes) -> dict[str, str]:
    """Return allowlisted certificate metadata without returning key material."""

    certificate = x509.load_pem_x509_certificate(content)
    return {
        "fingerprint": certificate.fingerprint(hashes.SHA256()).hex(":").upper(),
        "expires_at": certificate.not_valid_after_utc.isoformat().replace("+00:00", "Z"),
    }
