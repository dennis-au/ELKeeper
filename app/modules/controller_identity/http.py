"""Controller identity/settings routes."""

from __future__ import annotations

from typing import Annotated, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator

from app.modules.platform import get_setting, require_user, set_setting


class ControllerSettingsInput(BaseModel):
    timezone: str = Field(min_length=1, max_length=64)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        timezone = value.strip()
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError as error:
            raise ValueError("Choose a valid IANA timezone") from error
        return timezone


class ControllerPasswordInput(BaseModel):
    password: str = Field(min_length=1, max_length=1024)


class ControllerKeyImportInput(ControllerPasswordInput):
    private_key: str = Field(min_length=64, max_length=32768)
    passphrase: str | None = Field(default=None, max_length=1024)


class KeyInstall(ControllerPasswordInput):
    """Re-authentication payload for activating the controller SSH key."""


def build_router(*, db_factory: Callable, audit_fn: Callable[[str, str, str, str], None], default_timezone: str = "UTC") -> APIRouter:
    router = APIRouter()

    def current_settings() -> dict[str, str]:
        return {"timezone": get_setting(db_factory, "timezone", default_timezone) or default_timezone}

    @router.get("/api/controller/settings")
    async def get_controller_settings(_: Annotated[str, Depends(require_user)]):
        return current_settings()

    @router.put("/api/controller/settings")
    async def update_controller_settings(input: ControllerSettingsInput, username: Annotated[str, Depends(require_user)]):
        set_setting(db_factory, "timezone", input.timezone)
        audit_fn(username, "controller_display_timezone_updated", "timezone", input.timezone)
        return current_settings()

    return router


def build_key_router(
    *,
    key_status: Callable[[], dict],
    verify_password: Callable[[str, str], None],
    generate_private_key: Callable[[], object],
    parse_private_key: Callable[[str, str | None], object],
    stage_key: Callable[[object, str], dict],
    candidate_activation: Callable[[], tuple[object | None, object]],
    activate_key: Callable[[object | None, object, str], None],
    audit_fn: Callable[[str, str, str, str], None],
) -> APIRouter:
    router = APIRouter()

    @router.get("/api/controller/ssh-key")
    async def controller_ssh_key(_: Annotated[str, Depends(require_user)]):
        return key_status()

    @router.post("/api/controller/ssh-key/generate")
    async def generate_controller_ssh_key(input: ControllerPasswordInput, username: Annotated[str, Depends(require_user)]):
        verify_password(username, input.password)
        key = stage_key(generate_private_key(), "generated")
        audit_fn(username, "controller_ssh_key_generated", key["key_id"], key["state"])
        return {"key": key, "status": key_status()}

    @router.post("/api/controller/ssh-key/import")
    async def import_controller_ssh_key(input: ControllerKeyImportInput, username: Annotated[str, Depends(require_user)]):
        verify_password(username, input.password)
        key = stage_key(parse_private_key(input.private_key, input.passphrase), "imported")
        audit_fn(username, "controller_ssh_key_imported", key["key_id"], key["state"])
        return {"key": key, "status": key_status()}

    @router.post("/api/controller/ssh-key/activate")
    async def activate_controller_ssh_key(input: ControllerPasswordInput, username: Annotated[str, Depends(require_user)]):
        verify_password(username, input.password)
        active, candidate = candidate_activation()
        activate_key(active, candidate, username)
        return key_status()

    return router
