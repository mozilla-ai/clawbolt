from backend.app.agent.file_store import (
    MemoryFact,
    get_memory_store,
)
from backend.app.config import settings


async def save_memory(
    user_id: int,
    key: str,
    value: str,
    category: str = "general",
    confidence: float = 1.0,
    source_message_id: int | None = None,
) -> MemoryFact:
    """Save or update a memory fact. If key exists for this user, update it."""
    store = get_memory_store(user_id)
    return await store.save_memory(
        key=key,
        value=value,
        category=category,
        confidence=confidence,
        source_message_id=source_message_id,
    )


async def recall_memories(
    user_id: int,
    query: str,
    category: str | None = None,
    limit: int = settings.memory_recall_limit,
) -> list[MemoryFact]:
    """Recall memories relevant to a query using keyword matching."""
    store = get_memory_store(user_id)
    return await store.recall_memories(query=query, category=category, limit=limit)


async def get_all_memories(
    user_id: int,
    category: str | None = None,
) -> list[MemoryFact]:
    """Get all memories for a user, optionally filtered by category."""
    store = get_memory_store(user_id)
    return await store.get_all_memories(category=category)


async def delete_memory(user_id: int, key: str) -> bool:
    """Delete a specific memory. Returns True if found and deleted."""
    store = get_memory_store(user_id)
    return await store.delete_memory(key=key)


async def build_memory_context(
    user_id: int,
    query: str | None = None,
) -> str:
    """Build a MEMORY.md-style text block for injection into the agent prompt."""
    store = get_memory_store(user_id)
    return await store.build_memory_context(query=query)
