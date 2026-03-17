"""Endpoints for viewing and managing memory (freeform MEMORY.md)."""

from fastapi import APIRouter, Depends

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
    return MemoryResponse(content=store.read_memory())


@router.put("/user/memory", response_model=MemoryResponse)
async def update_memory(
    body: MemoryUpdate,
    current_user: User = Depends(get_current_user),
) -> MemoryResponse:
    """Overwrite MEMORY.md with new content."""
    store = get_memory_store(current_user.id)
    store.write_memory(body.content)
    return MemoryResponse(content=store.read_memory())
