"""Thin wrappers around MemoryStore for freeform memory access."""

from backend.app.agent.memory_db import get_memory_store


async def build_memory_context(user_id: str) -> str:
    """Build memory context text for injection into the agent prompt."""
    store = get_memory_store(user_id)
    return await store.build_memory_context()


def read_memory(user_id: str) -> str:
    """Read raw MEMORY.md content for a user."""
    store = get_memory_store(user_id)
    return store.read_memory()


def write_memory(user_id: str, content: str) -> None:
    """Write raw MEMORY.md content for a user."""
    store = get_memory_store(user_id)
    store.write_memory(content)
