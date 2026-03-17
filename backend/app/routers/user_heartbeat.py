"""Endpoints for managing heartbeat items."""

from fastapi import APIRouter, Depends, HTTPException

from backend.app.agent.file_store import HeartbeatStore
from backend.app.auth.dependencies import get_current_user
from backend.app.models import User
from backend.app.schemas import (
    HeartbeatCreateRequest,
    HeartbeatItemResponse,
    HeartbeatUpdateRequest,
)

router = APIRouter()


@router.get("/user/heartbeat", response_model=list[HeartbeatItemResponse])
async def list_heartbeat(
    current_user: User = Depends(get_current_user),
) -> list[HeartbeatItemResponse]:
    """List active heartbeat items."""
    store = HeartbeatStore(current_user.id)
    items = await store.get_heartbeat_items()
    return [
        HeartbeatItemResponse(
            id=item.id,
            description=item.description,
            schedule=item.schedule,
            status=item.status,
            created_at=item.created_at,
        )
        for item in items
    ]


@router.post("/user/heartbeat", response_model=HeartbeatItemResponse, status_code=201)
async def create_heartbeat_item(
    body: HeartbeatCreateRequest,
    current_user: User = Depends(get_current_user),
) -> HeartbeatItemResponse:
    """Add a new heartbeat item."""
    store = HeartbeatStore(current_user.id)
    item = await store.add_heartbeat_item(
        description=body.description,
        schedule=body.schedule,
    )
    return HeartbeatItemResponse(
        id=item.id,
        description=item.description,
        schedule=item.schedule,
        status=item.status,
        created_at=item.created_at,
    )


@router.put("/user/heartbeat/{item_id}", response_model=HeartbeatItemResponse)
async def update_heartbeat_item(
    item_id: str,
    body: HeartbeatUpdateRequest,
    current_user: User = Depends(get_current_user),
) -> HeartbeatItemResponse:
    """Update a heartbeat item."""
    store = HeartbeatStore(current_user.id)
    updated = await store.update_heartbeat_item(
        item_id,
        description=body.description,
        schedule=body.schedule,
        status=body.status,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Heartbeat item not found")
    return HeartbeatItemResponse(
        id=updated.id,
        description=updated.description,
        schedule=updated.schedule,
        status=updated.status,
        created_at=updated.created_at,
    )


@router.delete("/user/heartbeat/{item_id}", status_code=204)
async def delete_heartbeat_item(
    item_id: str,
    current_user: User = Depends(get_current_user),
) -> None:
    """Remove a heartbeat item."""
    store = HeartbeatStore(current_user.id)
    deleted = await store.delete_heartbeat_item(item_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Heartbeat item not found")
