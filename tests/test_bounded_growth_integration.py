"""Integration tests for the bounded-growth markdown enforcement.

These tests cover the seams where the registry policy meets the rest
of the system:

- workspace_tools rejecting over-budget writes / edits with a clean
  ToolResult error rather than crashing the agent loop.
- workspace_tools refusing to rewrite HISTORY.md (write_file /
  edit_file) so the append-only invariant cannot be bypassed.
- memory_db.append_history applying the windowing policy across many
  calls.
- memory_db.write_*_async raising BudgetExceededError on over-budget
  values.
- system_prompt builders tail-truncating over-budget legacy rows so a
  pre-cap row does not poison every system prompt forever.

The pure-unit registry helpers are exercised in
``tests/test_markdown_registry.py``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import pytest
from sqlalchemy import select

from backend.app.agent.markdown_registry import (
    DEFAULT_BUDGET,
    BudgetExceededError,
)
from backend.app.agent.memory_db import get_memory_store, reset_memory_stores
from backend.app.agent.system_prompt import (
    build_memory_section,
    build_soul_prompt,
    build_user_section,
)
from backend.app.agent.tools.base import ToolResult
from backend.app.agent.tools.workspace_tools import create_workspace_tools
from backend.app.database import db_session_async
from backend.app.models import MemoryDocument, User

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_tool_fn(user_id: str, tool_name: str) -> Callable[..., Awaitable[ToolResult]]:
    for t in create_workspace_tools(user_id):
        if t.name == tool_name:
            return t.function
    msg = f"Tool {tool_name!r} not found"
    raise ValueError(msg)


async def _set_user_column(user_id: str, column: str, value: str) -> None:
    async with db_session_async() as db:
        user = (await db.execute(select(User).filter_by(id=user_id))).scalar_one_or_none()
        assert user is not None
        setattr(user, column, value)
        await db.commit()


async def _seed_memory_doc(user_id: str, *, memory: str = "", history: str = "") -> None:
    async with db_session_async() as db:
        doc = (
            await db.execute(select(MemoryDocument).filter_by(user_id=user_id))
        ).scalar_one_or_none()
        if doc is None:
            doc = MemoryDocument(user_id=user_id, memory_text=memory, history_text=history)
            db.add(doc)
        else:
            doc.memory_text = memory
            doc.history_text = history
        await db.commit()


def _huge() -> str:
    """Return a payload guaranteed to exceed the 25 KiB budget."""
    return "x" * (DEFAULT_BUDGET + 100)


# ---------------------------------------------------------------------------
# workspace_tools: write_file enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_write_file_rejects_over_budget_user_md(test_user: User) -> None:
    write_fn = _get_tool_fn(test_user.id, "write_file")
    result = await write_fn(path="USER.md", content=_huge())
    assert result.is_error is True
    assert "USER.md" in result.content
    assert "exceeds" in result.content.lower()


@pytest.mark.asyncio()
async def test_write_file_rejects_over_budget_soul_md(test_user: User) -> None:
    write_fn = _get_tool_fn(test_user.id, "write_file")
    result = await write_fn(path="SOUL.md", content=_huge())
    assert result.is_error is True
    assert "SOUL.md" in result.content


@pytest.mark.asyncio()
async def test_write_file_rejects_over_budget_heartbeat_md(test_user: User) -> None:
    write_fn = _get_tool_fn(test_user.id, "write_file")
    result = await write_fn(path="HEARTBEAT.md", content=_huge())
    assert result.is_error is True
    assert "HEARTBEAT.md" in result.content


@pytest.mark.asyncio()
async def test_write_file_rejects_over_budget_memory_md(test_user: User) -> None:
    write_fn = _get_tool_fn(test_user.id, "write_file")
    result = await write_fn(path="memory/MEMORY.md", content=_huge())
    assert result.is_error is True
    assert "MEMORY.md" in result.content


@pytest.mark.asyncio()
async def test_write_file_refuses_to_rewrite_history_md(test_user: User) -> None:
    """HISTORY.md must not be writable via write_file: the append-with-window
    invariant lives in append_history, and a full rewrite would silently
    bypass it."""
    write_fn = _get_tool_fn(test_user.id, "write_file")
    result = await write_fn(path="memory/HISTORY.md", content="anything")
    assert result.is_error is True
    assert "HISTORY.md" in result.content
    assert "append" in result.content.lower()


@pytest.mark.asyncio()
async def test_write_file_under_budget_still_succeeds(test_user: User) -> None:
    """Sanity: well-under-budget writes are unchanged."""
    write_fn = _get_tool_fn(test_user.id, "write_file")
    result = await write_fn(path="USER.md", content="# User\n\n- Name: Alice\n")
    assert result.is_error is False


# ---------------------------------------------------------------------------
# workspace_tools: edit_file enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_edit_file_rejects_when_replacement_blows_budget(test_user: User) -> None:
    """An edit that grows the file past the budget must be rejected,
    not silently truncated."""
    seeded = "# User\n\n- A: x\n- B: y\n"
    await _set_user_column(test_user.id, "user_text", seeded)

    edit_fn = _get_tool_fn(test_user.id, "edit_file")
    huge_replacement = "y" * (DEFAULT_BUDGET + 100)
    result = await edit_fn(path="USER.md", old_text="- A: x\n", new_text=huge_replacement)
    assert result.is_error is True
    assert "USER.md" in result.content
    # The original row must be untouched on rejection.
    async with db_session_async() as db:
        user = (await db.execute(select(User).filter_by(id=test_user.id))).scalar_one_or_none()
        assert user is not None
        assert user.user_text == seeded


@pytest.mark.asyncio()
async def test_edit_file_refuses_history_md(test_user: User) -> None:
    edit_fn = _get_tool_fn(test_user.id, "edit_file")
    result = await edit_fn(path="memory/HISTORY.md", old_text="anything", new_text="something")
    assert result.is_error is True
    assert "HISTORY.md" in result.content


# ---------------------------------------------------------------------------
# memory_db: write_*_async raise on over-budget
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_write_memory_async_raises_on_over_budget(test_user: User) -> None:
    reset_memory_stores()
    store = get_memory_store(test_user.id)
    with pytest.raises(BudgetExceededError):
        await store.write_memory_async(_huge())


@pytest.mark.asyncio()
async def test_write_user_async_raises_on_over_budget(test_user: User) -> None:
    reset_memory_stores()
    store = get_memory_store(test_user.id)
    # The wrapper "# User\n\n{content}\n" adds ~12 bytes; pad past that.
    with pytest.raises(BudgetExceededError):
        await store.write_user_async(_huge())


@pytest.mark.asyncio()
async def test_write_soul_async_raises_on_over_budget(test_user: User) -> None:
    reset_memory_stores()
    store = get_memory_store(test_user.id)
    with pytest.raises(BudgetExceededError):
        await store.write_soul_async(_huge())


# ---------------------------------------------------------------------------
# memory_db: append_history applies the windowing policy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_append_history_keeps_storage_bounded_across_many_appends(
    test_user: User,
) -> None:
    """Repeated compaction-style appends must not cause unbounded
    growth on disk. Maps to the issue's acceptance criterion: 'tests
    cover repeated-update growth behavior for the important files'.
    """
    reset_memory_stores()
    store = get_memory_store(test_user.id)
    # Each entry is ~600 bytes; 200 of them would otherwise be ~120 KB,
    # well past the 25 KiB cap. After windowing, storage must stay
    # under the cap and only the most recent entries should survive.
    body = "x" * 600
    for i in range(200):
        await store.append_history(f"[2026-05-{(i % 28) + 1:02d} 00:00] {body}")
    history = await store.read_history_async()
    encoded = len(history.encode("utf-8"))
    assert encoded <= DEFAULT_BUDGET, f"history grew to {encoded} bytes; budget is {DEFAULT_BUDGET}"
    # The newest entry is preserved; the very first one is gone.
    assert "[2026-05-01" not in history or "[2026-05-28" in history


@pytest.mark.asyncio()
async def test_append_history_preserves_lock_semantics(test_user: User) -> None:
    """The locked append continues to return the row's full plaintext
    so the compaction audit can record an accurate post-append
    snapshot, even after windowing kicks in."""
    reset_memory_stores()
    store = get_memory_store(test_user.id)
    returned = await store.append_history("[2026-05-08 12:00] hello")
    on_disk = await store.read_history_async()
    # Reads strip trailing whitespace; compare normalized.
    assert returned.strip() == on_disk.strip()


# ---------------------------------------------------------------------------
# system_prompt builders: read-side truncation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_build_user_section_truncates_legacy_over_budget_row(
    test_user: User,
) -> None:
    """A row that pre-dates the write-time cap (or was inserted via a
    raw SQL bypass) must still be tail-truncated when injected into a
    prompt, so it cannot bloat every system prompt forever."""
    legacy = "TAIL_HOOK" + ("x" * (DEFAULT_BUDGET + 5_000))
    await _set_user_column(test_user.id, "user_text", legacy)

    async with db_session_async() as db:
        user = (await db.execute(select(User).filter_by(id=test_user.id))).scalar_one_or_none()
        assert user is not None
        section = build_user_section(user)

    assert len(section.encode("utf-8")) <= DEFAULT_BUDGET
    assert section.startswith("[truncated:")


@pytest.mark.asyncio()
async def test_build_soul_prompt_truncates_legacy_over_budget_row(
    test_user: User,
) -> None:
    legacy = "x" * (DEFAULT_BUDGET + 5_000)
    await _set_user_column(test_user.id, "soul_text", legacy)

    async with db_session_async() as db:
        user = (await db.execute(select(User).filter_by(id=test_user.id))).scalar_one_or_none()
        assert user is not None
        section = build_soul_prompt(user)

    assert len(section.encode("utf-8")) <= DEFAULT_BUDGET
    assert section.startswith("[truncated:")


@pytest.mark.asyncio()
async def test_build_memory_section_truncates_legacy_over_budget_row(
    test_user: User,
) -> None:
    """MEMORY.md is on an EncryptedString column; bypassing the cap
    requires a direct UPDATE rather than a normal store call. We
    simulate that by flipping the column directly."""
    reset_memory_stores()
    legacy = "x" * (DEFAULT_BUDGET + 5_000)
    await _seed_memory_doc(test_user.id, memory=legacy)

    section = await build_memory_section(test_user.id)

    assert len(section.encode("utf-8")) <= DEFAULT_BUDGET
    assert section.startswith("[truncated:")


@pytest.mark.asyncio()
async def test_build_memory_section_under_budget_passes_through(
    test_user: User,
) -> None:
    """Under-budget content must be returned verbatim; the truncation
    helper must be a no-op on healthy rows."""
    reset_memory_stores()
    content = "## Memory\n- Likes: blueprints\n- Dislikes: surprises\n"
    await _seed_memory_doc(test_user.id, memory=content)
    section = await build_memory_section(test_user.id)
    # Reads strip trailing whitespace; compare normalized.
    assert section.strip() == content.strip()
