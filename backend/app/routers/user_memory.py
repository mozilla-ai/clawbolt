"""Endpoints for viewing and managing memory (freeform MEMORY.md)."""

from fastapi import APIRouter, Depends, HTTPException, status

from backend.app.agent.markdown_registry import BudgetExceededError
from backend.app.agent.memory_db import get_memory_store
from backend.app.auth.dependencies import get_current_user
from backend.app.models import User
from backend.app.schemas import MemoryResponse, MemoryUpdate

router = APIRouter()


@router.get("/user/memory", response_model=MemoryResponse)
async def get_memory(
    current_user: User = Depends(get_current_user),
) -> MemoryResponse:
    """Return the raw MEMORY.md content."""
    store = get_memory_store(current_user.id)
    return MemoryResponse(content=await store.read_memory_async())


@router.put("/user/memory", response_model=MemoryResponse)
async def update_memory(
    body: MemoryUpdate,
    current_user: User = Depends(get_current_user),
) -> MemoryResponse:
    """Overwrite MEMORY.md with new content.

    Returns ``413 Payload Too Large`` when *body.content* exceeds the
    bounded-growth byte budget for ``MEMORY.md`` (see
    :mod:`backend.app.agent.markdown_registry`). The original message
    from the registry includes the actual size and the budget so a
    user-side editor can show a useful error.
    """
    store = get_memory_store(current_user.id)
    try:
        await store.write_memory_async(body.content)
    except BudgetExceededError as exc:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=str(exc),
        ) from exc
    return MemoryResponse(content=await store.read_memory_async())
