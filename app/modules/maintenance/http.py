"""Maintenance capability route owned by the maintenance module."""

from __future__ import annotations

from typing import Annotated, Mapping

from fastapi import APIRouter, Depends

from app.modules.platform import require_user


def build_router(capabilities: Mapping[str, object]) -> APIRouter:
    router = APIRouter()

    @router.get("/api/maintenance/capabilities")
    async def maintenance_capabilities(_: Annotated[str, Depends(require_user)]):
        return {
            "planning": capabilities["planning"],
            "operations": {
                "manual_maintenance_entry": capabilities["manual_maintenance_entry"],
                "container_stop": capabilities["container_stop"],
                "host_shutdown": capabilities["host_shutdown"],
                "host_reboot": capabilities["host_reboot"],
                "rolling_restart": capabilities["rolling_restart"],
                "upgrade": capabilities["upgrade"],
                "evacuation": capabilities["evacuation"],
            },
            "lifecycle": {
                "manual_maintenance_exit": capabilities["manual_maintenance_exit"],
                "recovery": capabilities["recovery"],
            },
            "backends": {
                "documented_rolling": True,
                "node_shutdown": capabilities["node_shutdown_backend"],
            },
        }

    return router
