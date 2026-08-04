"""SSH key parsing and serialization owned by controller identity."""

from __future__ import annotations

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from fastapi import HTTPException

from .contracts import public_key_fingerprint


def key_algorithm(private_key) -> str:
    if isinstance(private_key, ed25519.Ed25519PrivateKey):
        return "ed25519"
    if isinstance(private_key, rsa.RSAPrivateKey):
        if private_key.key_size < 3072:
            raise HTTPException(422, "Imported RSA keys must be at least 3072 bits")
        return "rsa"
    if isinstance(private_key, ec.EllipticCurvePrivateKey):
        return "ecdsa"
    raise HTTPException(422, "Only Ed25519, ECDSA, and RSA SSH private keys are supported")


def serialize_private_key(private_key) -> str:
    return private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.OpenSSH,
        serialization.NoEncryption(),
    ).decode()


def key_material(private_key) -> tuple[str, str, str, str]:
    algorithm = key_algorithm(private_key)
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.OpenSSH,
        serialization.PublicFormat.OpenSSH,
    ).decode()
    return serialize_private_key(private_key), public_key, public_key_fingerprint(public_key), algorithm


def parse_imported_private_key(value: str, passphrase: str | None = None):
    try:
        return serialization.load_ssh_private_key(
            value.encode(), passphrase.encode() if passphrase else None
        )
    except (TypeError, ValueError) as error:
        raise HTTPException(422, "Import a valid OpenSSH private key and matching passphrase") from error


def normalize_ssh_host_key(value: str) -> str:
    if not value or not value.strip():
        return ""
    parts = value.strip().split()
    if len(parts) >= 3 and not parts[0].startswith(("ssh-", "ecdsa-")):
        parts = parts[1:]
    if len(parts) < 2:
        raise HTTPException(422, "Provide an OpenSSH host public key")
    normalized = " ".join(parts[:2])
    try:
        serialization.load_ssh_public_key(normalized.encode())
    except ValueError as error:
        raise HTTPException(422, "Provide a valid OpenSSH host public key") from error
    return normalized
