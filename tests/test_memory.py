import pytest

from backend.app.agent.memory_db import (
    build_memory_context,
    get_memory_store,
    read_memory,
    write_memory,
)
from backend.app.models import User


@pytest.mark.asyncio()
async def test_write_and_read_memory(test_user: User) -> None:
    """write_memory / read_memory should round-trip freeform content."""
    write_memory(test_user.id, "## Pricing\n- Deck: $45/sqft")
    content = read_memory(test_user.id)
    assert "Deck: $45/sqft" in content


@pytest.mark.asyncio()
async def test_read_memory_empty(test_user: User) -> None:
    """read_memory returns empty string when no MEMORY.md exists."""
    content = read_memory(test_user.id)
    assert content == ""


@pytest.mark.asyncio()
async def test_write_memory_overwrites(test_user: User) -> None:
    """write_memory should fully replace the file."""
    write_memory(test_user.id, "old content")
    write_memory(test_user.id, "new content")
    content = read_memory(test_user.id)
    assert "new content" in content
    assert "old content" not in content


@pytest.mark.asyncio()
async def test_build_memory_context_with_memory(test_user: User) -> None:
    """build_memory_context should include memory text."""
    store = get_memory_store(test_user.id)
    store.write_memory("## Pricing\n- Deck: $35/sqft")

    context = await build_memory_context(test_user.id)
    assert "$35/sqft" in context


@pytest.mark.asyncio()
async def test_build_memory_context_empty(test_user: User) -> None:
    """build_memory_context returns empty string when no memory."""
    context = await build_memory_context(test_user.id)
    assert context == ""
