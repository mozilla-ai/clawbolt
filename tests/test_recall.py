"""Tests for memory recall behavior via the memory context."""

import pytest

from backend.app.agent.memory_db import build_memory_context, get_memory_store
from backend.app.models import User


@pytest.mark.asyncio()
async def test_build_memory_context_includes_memory_content(
    test_user: User,
) -> None:
    """build_memory_context should include MEMORY.md content."""
    store = get_memory_store(test_user.id)
    store.write_memory("## Pricing\n- Hourly rate: $75/hour for general work")

    context = await build_memory_context(test_user.id)
    assert "$75/hour" in context


@pytest.mark.asyncio()
async def test_build_memory_context_empty(test_user: User) -> None:
    """build_memory_context returns empty string when no memory exists."""
    context = await build_memory_context(test_user.id)
    assert context == ""
