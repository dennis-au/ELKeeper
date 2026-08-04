"""Credential hashing, encryption, and redaction primitives."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os

from cryptography.fernet import Fernet, InvalidToken


class StoredSecretError(ValueError):
    """Raised when encrypted controller material cannot be opened."""


def digest(value: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    derived = hashlib.scrypt(value.encode(), salt=salt, n=16384, r=8, p=1)
    return base64.urlsafe_b64encode(salt + derived).decode()


def valid_password(value: str, stored: str) -> bool:
    try:
        raw = base64.urlsafe_b64decode(stored)
        return hmac.compare_digest(digest(value, raw[:16]), stored)
    except (ValueError, TypeError):
        return False


def _cipher(key: str | None = None) -> Fernet:
    material = key if key is not None else os.getenv("APP_SECRET_KEY", "")
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(material.encode()).digest()))


def seal_config(value: str, key: str | None = None) -> str:
    return _cipher(key).encrypt(value.encode()).decode()


def open_config(value: str, key: str | None = None):
    try:
        return json.loads(_cipher(key).decrypt(value.encode()).decode())
    except (InvalidToken, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        # Legacy rows were stored as plain JSON.  Compatibility reads are
        # intentionally one-way; callers can rewrite them encrypted.
        return json.loads(value)


def seal_secret(value: str, key: str | None = None) -> str:
    return _cipher(key).encrypt(value.encode()).decode()


def open_secret(value: str, key: str | None = None) -> str:
    try:
        return _cipher(key).decrypt(value.encode()).decode()
    except (InvalidToken, ValueError, UnicodeDecodeError) as error:
        raise StoredSecretError("Stored controller credential could not be decrypted") from error


def redact(value: object) -> object:
    """Return a recursively redacted copy for logs and API diagnostics."""

    secret_names = {"password", "token", "secret", "private_key", "credential", "passphrase"}
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if key.lower() in secret_names else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def redact_config(value: dict) -> dict:
    """Mask configured secret-like values while preserving presence metadata."""

    hidden = {"password", "token", "secret", "api_key", "apikey", "key", "credential"}
    result = {}
    for name, item in value.items():
        if any(part in name.lower() for part in hidden):
            result[name] = "configured" if item else ""
        elif isinstance(item, dict):
            result[name] = redact_config(item)
        else:
            result[name] = item
    return result
