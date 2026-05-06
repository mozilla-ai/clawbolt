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
    await write_memory(test_user.id, "## Pricing\n- Deck: $45/sqft")
    content = await read_memory(test_user.id)
    assert "Deck: $45/sqft" in content


@pytest.mark.asyncio()
async def test_read_memory_empty(test_user: User) -> None:
    """read_memory returns empty string when no MEMORY.md exists."""
    content = await read_memory(test_user.id)
    assert content == ""


@pytest.mark.asyncio()
async def test_write_memory_overwrites(test_user: User) -> None:
    """write_memory should fully replace the file."""
    await write_memory(test_user.id, "old content")
    await write_memory(test_user.id, "new content")
    content = await read_memory(test_user.id)
    assert "new content" in content
    assert "old content" not in content


@pytest.mark.asyncio()
async def test_build_memory_context_with_memory(test_user: User) -> None:
    """build_memory_context should include memory text."""
    store = get_memory_store(test_user.id)
    await store.write_memory_async("## Pricing\n- Deck: $35/sqft")

    context = await build_memory_context(test_user.id)
    assert "$35/sqft" in context


@pytest.mark.asyncio()
async def test_build_memory_context_empty(test_user: User) -> None:
    """build_memory_context returns empty string when no memory."""
    context = await build_memory_context(test_user.id)
    assert context == ""


@pytest.mark.asyncio()
async def test_append_history_multi_append_round_trips(test_user: User) -> None:
    """Sequential ``append_history`` calls all round-trip through ``read_history``.

    Regression test for the encrypted-history concat bug.
    ``MemoryDocument.history_text`` is an ``EncryptedString`` column,
    so the original SQL-level ``history_text + suffix`` builder
    concatenated ciphertext envelopes and broke decryption on read
    after the second append. The fix reads the row under
    ``SELECT ... FOR UPDATE``, concatenates plaintext in Python, and
    rewrites the column with a fresh envelope.
    """
    store = get_memory_store(test_user.id)
    await store.append_history("first entry")
    await store.append_history("second entry")
    await store.append_history("third entry")

    history = await store.read_history_async()
    # ``read_history`` strips trailing whitespace, so the final entry's
    # newline is gone but the inter-entry newlines remain.
    assert history == "first entry\nsecond entry\nthird entry"


@pytest.mark.asyncio()
async def test_append_history_after_seed_round_trips(test_user: User) -> None:
    """Appending against a row that already exists keeps every prior entry.

    Targets the second-and-later append path (``UPDATE`` branch), as
    opposed to the create-on-first-append branch. The pre-fix builder
    silently corrupted ``history_text`` here because SQL-side
    concatenation glued two ciphertext envelopes together.
    """
    store = get_memory_store(test_user.id)
    await store.append_history("seed")
    await store.append_history("follow-up")

    assert await store.read_history_async() == "seed\nfollow-up"
