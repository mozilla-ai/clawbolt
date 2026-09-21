"""Admin console API.

One module per area of the console. Each declares a bare ``APIRouter``;
this module mounts them under ``/admin`` in the order the endpoints were
originally declared, so path resolution is unchanged.
"""

from __future__ import annotations

from fastapi import APIRouter

from backend.app.routers.admin import (
    access,
    api_keys,
    channels,
    context,
    diagnostics,
    llm_config,
    overview,
    usage,
    users,
)

router = APIRouter(prefix="/admin", tags=["admin"])

for _sub in (
    users,
    context,
    diagnostics,
    usage,
    access,
    overview,
    channels,
    llm_config,
    api_keys,
):
    router.include_router(_sub.router)

__all__ = ["router"]
