"""Safe metadata operations for controller-owned SSH identity."""

from __future__ import annotations

import base64
import binascii
import hashlib


def public_key_fingerprint(public_key: str) -> str:
    """Return an OpenSSH SHA256 fingerprint without exposing key material."""

    try:
        encoded = public_key.strip().split()[1]
        digest_value = hashlib.sha256(base64.b64decode(encoded.encode())).digest()
    except (IndexError, ValueError, binascii.Error) as error:
        raise ValueError("Invalid OpenSSH public key") from error
    return "SHA256:" + base64.b64encode(digest_value).decode().rstrip("=")
