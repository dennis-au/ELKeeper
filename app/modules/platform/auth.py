"""Browser authentication dependency shared by feature routers."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import time
from typing import Callable

from fastapi import HTTPException, Request


AUTH_TOKEN_TTL_SECONDS = 28800
RUN_EVENTS_TOKEN_TTL_SECONDS = 600


def token_piece(value: bytes) -> str:
    """Encode signed-token payload segments without padding."""

    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def read_token_piece(value: str) -> bytes:
    """Decode a padded or unpadded signed-token payload segment."""

    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _key_material(key: str | None) -> bytes:
    return (key if key is not None else os.getenv("APP_SECRET_KEY", "")).encode()


def _signed_payload(payload: bytes, *, key: str | None = None) -> str:
    signature = hmac.new(_key_material(key), payload, hashlib.sha256).digest()
    return f"{token_piece(payload)}.{token_piece(signature)}"


def _verified_payload(token: str, *, key: str | None = None) -> bytes | None:
    try:
        payload_text, signature_text = token.split(".", 1)
        payload = read_token_piece(payload_text)
        signature = read_token_piece(signature_text)
        expected = hmac.new(_key_material(key), payload, hashlib.sha256).digest()
        return payload if hmac.compare_digest(signature, expected) else None
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return None


def signed_token(username: str, *, key: str | None = None, now: float | None = None) -> str:
    issued = str(int(time.time() if now is None else now))
    return _signed_payload(f"{username}:{issued}".encode(), key=key)


def token_user(token: str, *, key: str | None = None, now: float | None = None) -> str | None:
    """Validate a controller bearer token without importing app assembly."""

    try:
        payload = _verified_payload(token, key=key)
        if payload is None:
            return None
        username, issued = payload.decode().rsplit(":", 1)
        current = time.time() if now is None else now
        if int(issued) + AUTH_TOKEN_TTL_SECONDS >= current:
            return username
    except (ValueError, UnicodeDecodeError):
        pass
    return None


def run_events_token(run_id: int, *, key: str | None = None, now: float | None = None) -> str:
    issued = str(int(time.time() if now is None else now))
    return _signed_payload(f"run-events:{run_id}:{issued}".encode(), key=key)


def valid_run_events_token(
    token: str,
    run_id: int,
    *,
    key: str | None = None,
    now: float | None = None,
) -> bool:
    try:
        payload = _verified_payload(token, key=key)
        if payload is None:
            return False
        prefix, signed_run_id, issued = payload.decode().split(":", 2)
        current = time.time() if now is None else now
        return (
            prefix == "run-events"
            and int(signed_run_id) == run_id
            and int(issued) + RUN_EVENTS_TOKEN_TTL_SECONDS >= current
        )
    except (ValueError, UnicodeDecodeError):
        return False


def signed_scope_token(scope: str, *, key: str | None = None, now: float | None = None) -> str:
    issued = str(int(time.time() if now is None else now))
    return _signed_payload(f"{scope}:{issued}".encode(), key=key)


def valid_scope_token(
    token: str,
    scope: str,
    *,
    key: str | None = None,
    ttl: int = RUN_EVENTS_TOKEN_TTL_SECONDS,
    now: float | None = None,
) -> bool:
    try:
        payload = _verified_payload(token, key=key)
        if payload is None:
            return False
        signed_scope, issued = payload.decode().rsplit(":", 1)
        current = time.time() if now is None else now
        return signed_scope == scope and int(issued) + ttl >= current
    except (ValueError, UnicodeDecodeError):
        return False


async def require_user(request: Request) -> str:
    """FastAPI dependency requiring a valid controller bearer token."""

    header = request.headers.get("authorization", "")
    username = token_user(header[7:]) if header.startswith("Bearer ") else None
    if not username:
        raise HTTPException(401, "Authentication required")
    return username


def password_matches(
    db_factory: Callable,
    username: str,
    password: str,
    valid_password: Callable[[str, str], bool],
) -> bool:
    """Check the current controller password through the platform boundary."""

    with db_factory() as connection:
        row = connection.execute(
            "SELECT password_hash FROM users WHERE username=?", (username,)
        ).fetchone()
    return bool(row and valid_password(password, row["password_hash"]))
