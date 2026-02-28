from typing import Any

from fastapi import APIRouter

from backend.app.auth.loader import get_auth_backend

router = APIRouter()


@router.get("/auth/config")
async def auth_config() -> dict[str, Any]:
    backend = get_auth_backend()
    if backend is None:
        return {"method": "none", "required": False}
    return backend.get_auth_config()
