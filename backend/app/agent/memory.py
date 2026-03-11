"""Thin wrappers around FileMemoryStore for freeform MEMORY.md access."""

from backend.app.agent.file_store import get_memory_store


async def build_memory_context(user_id: int) -> str:
    """Build memory context text for injection into the agent prompt."""
    store = get_memory_store(user_id)
    return await store.build_memory_context()


def read_memory(user_id: int) -> str:
    """Read raw MEMORY.md content for a user."""
    store = get_memory_store(user_id)
    return store.read_memory()


def write_memory(user_id: int, content: str) -> None:
    """Write raw MEMORY.md content for a user."""
    store = get_memory_store(user_id)
    store.write_memory(content)
