"""Endpoints for viewing and managing tool permissions (PERMISSIONS.json)."""

import json

from fastapi import APIRouter, Depends, HTTPException

from backend.app.agent.approval import get_approval_store
from backend.app.auth.dependencies import get_current_user
from backend.app.models import User
from backend.app.schemas import PermissionsResponse, PermissionsUpdate

router = APIRouter()


@router.get("/user/permissions", response_model=PermissionsResponse)
async def get_permissions(
    current_user: User = Depends(get_current_user),
) -> PermissionsResponse:
    """Return the current PERMISSIONS.json content."""
    store = get_approval_store()
    data = store.ensure_complete(current_user.id)
    return PermissionsResponse(content=json.dumps(data, indent=2))


@router.put("/user/permissions", response_model=PermissionsResponse)
async def update_permissions(
    body: PermissionsUpdate,
    current_user: User = Depends(get_current_user),
) -> PermissionsResponse:
    """Overwrite PERMISSIONS.json with new content."""
    try:
        data = json.loads(body.content)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Permissions must be a JSON object")

    _validate_permissions_shape(data)

    store = get_approval_store()
    store._save(current_user.id, data)
    return PermissionsResponse(content=json.dumps(data, indent=2))


_VALID_LEVELS = {"always", "ask", "deny"}


def _validate_permissions_shape(data: dict[str, object]) -> None:
    """Validate that tools/resources contain only valid permission levels."""
    tools = data.get("tools")
    if tools is not None:
        if not isinstance(tools, dict):
            raise HTTPException(status_code=400, detail="'tools' must be an object")
        bad = [str(k) for k, v in tools.items() if not isinstance(v, str) or v not in _VALID_LEVELS]
        if bad:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid permission level for: {', '.join(bad[:5])}. "
                f"Allowed values: always, ask, deny",
            )

    resources = data.get("resources")
    if resources is not None:
        if not isinstance(resources, dict):
            raise HTTPException(status_code=400, detail="'resources' must be an object")
        for tool_name, res_map in resources.items():
            if not isinstance(res_map, dict):
                raise HTTPException(
                    status_code=400,
                    detail=f"'resources.{tool_name}' must be an object",
                )
            bad = [
                str(k)
                for k, v in res_map.items()
                if not isinstance(v, str) or v not in _VALID_LEVELS
            ]
            if bad:
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid permission level in resources.{tool_name}: "
                    f"{', '.join(bad[:5])}. Allowed values: always, ask, deny",
                )
