"""Endpoints for managing checklist items."""

from fastapi import APIRouter, Depends, HTTPException

from backend.app.agent.file_store import ContractorData, HeartbeatStore
from backend.app.auth.dependencies import get_current_user
from backend.app.schemas import (
    ChecklistCreateRequest,
    ChecklistItemResponse,
    ChecklistUpdateRequest,
)

router = APIRouter()


@router.get("/user/checklist", response_model=list[ChecklistItemResponse])
async def list_checklist(
    current_user: ContractorData = Depends(get_current_user),
) -> list[ChecklistItemResponse]:
    """List active checklist items."""
    store = HeartbeatStore(current_user.id)
    items = await store.get_checklist()
    return [
        ChecklistItemResponse(
            id=item.id,
            description=item.description,
            schedule=item.schedule,
            status=item.status,
            created_at=item.created_at,
        )
        for item in items
    ]


@router.post("/user/checklist", response_model=ChecklistItemResponse, status_code=201)
async def create_checklist_item(
    body: ChecklistCreateRequest,
    current_user: ContractorData = Depends(get_current_user),
) -> ChecklistItemResponse:
    """Add a new checklist item."""
    store = HeartbeatStore(current_user.id)
    item = await store.add_checklist_item(
        description=body.description,
        schedule=body.schedule,
    )
    return ChecklistItemResponse(
        id=item.id,
        description=item.description,
        schedule=item.schedule,
        status=item.status,
        created_at=item.created_at,
    )


@router.put("/user/checklist/{item_id}", response_model=ChecklistItemResponse)
async def update_checklist_item(
    item_id: int,
    body: ChecklistUpdateRequest,
    current_user: ContractorData = Depends(get_current_user),
) -> ChecklistItemResponse:
    """Update a checklist item."""
    store = HeartbeatStore(current_user.id)
    updated = await store.update_checklist_item(
        item_id,
        description=body.description,
        schedule=body.schedule,
        status=body.status,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Checklist item not found")
    return ChecklistItemResponse(
        id=updated.id,
        description=updated.description,
        schedule=updated.schedule,
        status=updated.status,
        created_at=updated.created_at,
    )


@router.delete("/user/checklist/{item_id}", status_code=204)
async def delete_checklist_item(
    item_id: int,
    current_user: ContractorData = Depends(get_current_user),
) -> None:
    """Remove a checklist item."""
    store = HeartbeatStore(current_user.id)
    deleted = await store.delete_checklist_item(item_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Checklist item not found")
