"""Sensitive-item catalog and audited reveal routes."""

from __future__ import annotations

import secrets as token_secrets
import time
from typing import Awaitable, Callable

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.modules.platform import require_user


class RevealGrantInput(BaseModel):
    cluster_id: int = Field(ge=1)
    password: str = Field(min_length=1, max_length=512)


class RevealInput(BaseModel):
    grant_token: str = Field(min_length=20, max_length=512)
    purpose: str = Field(default="reveal", pattern=r"^(reveal|copy)$")


def build_router(
    *,
    catalog_provider: Callable[[int], tuple[object, dict, list[dict]]],
    metadata_provider: Callable[[dict], Awaitable[dict]],
    read_remote: Callable[..., Awaitable[bytes]],
    verify_reauthentication: Callable[[str, str], None],
    audit_fn: Callable[[str, str, int, str], None],
    user_dependency: Callable = require_user,
    grant_ttl: int = 60,
) -> APIRouter:
    router = APIRouter()
    reveal_grants: dict[str, dict[str, object]] = {}

    @router.get("/api/clusters/{cluster_id}/sensitive-items")
    async def sensitive_items(cluster_id: int, _: str = Depends(user_dependency)):
        _, _, catalog = catalog_provider(cluster_id)
        enriched = await __import__("asyncio").gather(*(metadata_provider(dict(item)) for item in catalog))
        public_items = []
        for item in enriched:
            public = {
                key: value
                for key, value in item.items()
                if key not in {"node", "path", "db_key", "certificate"}
            }
            if item["category"] in {"Certificates", "Private keys"}:
                public["storage_path"] = item["path"]
            public["masked_value"] = "********"
            public_items.append(public)
        return {"items": public_items}

    @router.post("/api/auth/reveal-grants")
    async def create_reveal_grant(input: RevealGrantInput, username: str = Depends(user_dependency)):
        catalog_provider(input.cluster_id)
        try:
            verify_reauthentication(username, input.password)
        except HTTPException:
            audit_fn(username, "reveal-grant-rejected", input.cluster_id, "")
            raise
        token = token_secrets.token_urlsafe(32)
        reveal_grants[token] = {
            "username": username,
            "cluster_id": input.cluster_id,
            "expires": time.time() + grant_ttl,
        }
        audit_fn(username, "reveal-grant-created", input.cluster_id, "")
        return {"grant_token": token, "expires_in": grant_ttl}

    @router.post("/api/clusters/{cluster_id}/sensitive-items/{item_id}/reveal")
    async def reveal_sensitive_item(
        cluster_id: int,
        item_id: str,
        input: RevealInput,
        username: str = Depends(user_dependency),
    ):
        grant = reveal_grants.get(input.grant_token)
        if (
            not grant
            or float(grant["expires"]) < time.time()
            or grant["username"] != username
            or grant["cluster_id"] != cluster_id
        ):
            raise HTTPException(403, "Reveal grant is missing, expired, or scoped to another cluster")
        _, credentials, catalog = catalog_provider(cluster_id)
        item = next((entry for entry in catalog if entry["id"] == item_id), None)
        if not item:
            raise HTTPException(404, "Sensitive item not found")
        if item.get("db_key"):
            value = credentials.get(item["db_key"], "")
        else:
            try:
                value = (await read_remote(item["node"], "cat", item["path"])).decode()
            except Exception as error:
                raise HTTPException(503, f"Sensitive item is unavailable: {str(error)[:160]}") from error
        if not value:
            raise HTTPException(404, "Sensitive item is not configured")
        audit_fn(username, input.purpose, cluster_id, item_id)
        return {"value": value, "hide_after": 30}

    return router


__all__ = ["RevealGrantInput", "RevealInput", "build_router"]
