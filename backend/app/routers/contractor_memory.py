"""Endpoints for viewing and managing memory facts."""

from fastapi import APIRouter, Depends, HTTPException

from backend.app.agent.file_store import ContractorData, get_memory_store
from backend.app.auth.dependencies import get_current_user
from backend.app.schemas import MemoryFactResponse, MemoryFactUpdate

router = APIRouter()


@router.get("/contractor/memory", response_model=list[MemoryFactResponse])
async def list_memory(
    category: str | None = None,
    current_user: ContractorData = Depends(get_current_user),
) -> list[MemoryFactResponse]:
    """List all memory facts, optionally filtered by category."""
    store = get_memory_store(current_user.id)
    facts = await store.get_all_memories(category=category)
    return [
        MemoryFactResponse(
            key=f.key,
            value=f.value,
            category=f.category,
            confidence=f.confidence,
        )
        for f in facts
    ]


@router.put("/contractor/memory/{key}", response_model=MemoryFactResponse)
async def update_memory(
    key: str,
    body: MemoryFactUpdate,
    current_user: ContractorData = Depends(get_current_user),
) -> MemoryFactResponse:
    """Update a memory fact's value, category, or confidence."""
    store = get_memory_store(current_user.id)
    existing = await store.get_all_memories()
    fact = next((f for f in existing if f.key == key), None)
    if fact is None:
        raise HTTPException(status_code=404, detail="Memory fact not found")

    value = body.value if body.value is not None else fact.value
    category = body.category if body.category is not None else fact.category
    confidence = body.confidence if body.confidence is not None else fact.confidence

    updated = await store.save_memory(
        key=key,
        value=value,
        category=category,
        confidence=confidence,
    )
    return MemoryFactResponse(
        key=updated.key,
        value=updated.value,
        category=updated.category,
        confidence=updated.confidence,
    )


@router.delete("/contractor/memory/{key}", status_code=204)
async def delete_memory(
    key: str,
    current_user: ContractorData = Depends(get_current_user),
) -> None:
    """Delete a memory fact."""
    store = get_memory_store(current_user.id)
    deleted = await store.delete_memory(key)
    if not deleted:
        raise HTTPException(status_code=404, detail="Memory fact not found")
