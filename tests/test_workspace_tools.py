"""Tests for generic workspace file tools (read_file, write_file, edit_file)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

import backend.app.database as _db_module
from backend.app.agent.tools.base import ToolResult
from backend.app.agent.tools.workspace_tools import create_workspace_tools
from backend.app.config import settings
from backend.app.models import MemoryDocument, User


def _get_tool_fn(user_id: str, tool_name: str) -> Callable[..., Awaitable[ToolResult]]:
    """Return the async function for the named tool."""
    tools = create_workspace_tools(user_id)
    for t in tools:
        if t.name == tool_name:
            return t.function
    msg = f"Tool {tool_name!r} not found"
    raise ValueError(msg)


def _user_dir(user: User) -> Path:
    return Path(settings.data_dir) / str(user.id)


def _set_user_column(user_id: str, column: str, value: str) -> None:
    """Set a text column on User directly in the DB."""
    db = _db_module.SessionLocal()
    try:
        user = db.query(User).filter_by(id=user_id).first()
        assert user is not None
        setattr(user, column, value)
        db.commit()
    finally:
        db.close()


def _get_user_column(user_id: str, column: str) -> str:
    """Read a text column from User in the DB."""
    db = _db_module.SessionLocal()
    try:
        user = db.query(User).filter_by(id=user_id).first()
        assert user is not None
        return getattr(user, column, "") or ""
    finally:
        db.close()


# --- read_file tests (DB-backed: USER.md, SOUL.md, HEARTBEAT.md) ---


@pytest.mark.asyncio()
async def test_read_file_success(test_user: User) -> None:
    """read_file should return user_text from DB for USER.md."""
    _set_user_column(test_user.id, "user_text", "# User\n\n- Name: Jake\n")

    read_fn = _get_tool_fn(test_user.id, "read_file")
    result = await read_fn(path="USER.md")
    assert result.is_error is False
    assert "Jake" in result.content


@pytest.mark.asyncio()
async def test_read_file_not_found(test_user: User) -> None:
    """read_file should return error for missing disk file."""
    read_fn = _get_tool_fn(test_user.id, "read_file")
    result = await read_fn(path="NONEXISTENT.md")
    assert result.is_error is True
    assert "not found" in result.content.lower()


@pytest.mark.asyncio()
async def test_read_file_rejects_non_markdown(test_user: User) -> None:
    """read_file should reject non-markdown files."""
    read_fn = _get_tool_fn(test_user.id, "read_file")
    result = await read_fn(path="user.json")
    assert result.is_error is True
    assert ".md" in result.content


@pytest.mark.asyncio()
async def test_read_file_rejects_path_traversal(test_user: User) -> None:
    """read_file should reject paths that escape the user directory."""
    read_fn = _get_tool_fn(test_user.id, "read_file")
    result = await read_fn(path="../../etc/passwd.md")
    assert result.is_error is True


# --- write_file tests (DB-backed for USER.md, disk for others) ---


@pytest.mark.asyncio()
async def test_write_file_db_backed(test_user: User) -> None:
    """write_file should write USER.md to the DB."""
    write_fn = _get_tool_fn(test_user.id, "write_file")
    result = await write_fn(path="USER.md", content="# User\n\n- Name: Sarah\n")
    assert result.is_error is False
    assert "Wrote" in result.content
    assert _get_user_column(test_user.id, "user_text") == "# User\n\n- Name: Sarah\n"


@pytest.mark.asyncio()
async def test_write_file_overwrites_db(test_user: User) -> None:
    """write_file should overwrite existing DB content."""
    _set_user_column(test_user.id, "user_text", "old content")

    write_fn = _get_tool_fn(test_user.id, "write_file")
    await write_fn(path="USER.md", content="new content")
    assert _get_user_column(test_user.id, "user_text") == "new content"


@pytest.mark.asyncio()
async def test_write_file_creates_subdirectory(test_user: User) -> None:
    """write_file should create parent directories for disk files."""
    cdir = _user_dir(test_user)
    cdir.mkdir(parents=True, exist_ok=True)

    write_fn = _get_tool_fn(test_user.id, "write_file")
    result = await write_fn(path="memory/NOTES.md", content="# Notes\n")
    assert result.is_error is False
    assert (cdir / "memory" / "NOTES.md").exists()


@pytest.mark.asyncio()
async def test_write_file_rejects_non_markdown(test_user: User) -> None:
    """write_file should reject non-markdown files."""
    write_fn = _get_tool_fn(test_user.id, "write_file")
    result = await write_fn(path="evil.json", content="{}")
    assert result.is_error is True


@pytest.mark.asyncio()
async def test_write_file_rejects_path_traversal(test_user: User) -> None:
    """write_file should reject paths that escape the user directory."""
    write_fn = _get_tool_fn(test_user.id, "write_file")
    result = await write_fn(path="../../../tmp/hack.md", content="nope")
    assert result.is_error is True


# --- edit_file tests (DB-backed for USER.md, disk for others) ---


@pytest.mark.asyncio()
async def test_edit_file_replaces_text(test_user: User) -> None:
    """edit_file should replace exact text in DB column."""
    _set_user_column(test_user.id, "user_text", "- Rate: $85/hr\n- Hours: 8-5\n")

    edit_fn = _get_tool_fn(test_user.id, "edit_file")
    result = await edit_fn(path="USER.md", old_text="$85/hr", new_text="$100/hr")
    assert result.is_error is False
    assert _get_user_column(test_user.id, "user_text") == "- Rate: $100/hr\n- Hours: 8-5\n"


@pytest.mark.asyncio()
async def test_edit_file_text_not_found(test_user: User) -> None:
    """edit_file should return error when old_text not found."""
    _set_user_column(test_user.id, "user_text", "- Name: Jake\n")

    edit_fn = _get_tool_fn(test_user.id, "edit_file")
    result = await edit_fn(path="USER.md", old_text="nonexistent text", new_text="replacement")
    assert result.is_error is True
    assert "not found" in result.content.lower()


@pytest.mark.asyncio()
async def test_edit_file_ambiguous_match(test_user: User) -> None:
    """edit_file should return error when old_text matches multiple times."""
    _set_user_column(test_user.id, "user_text", "foo bar\nfoo baz\n")

    edit_fn = _get_tool_fn(test_user.id, "edit_file")
    result = await edit_fn(path="USER.md", old_text="foo", new_text="qux")
    assert result.is_error is True
    assert "2 matches" in result.content


@pytest.mark.asyncio()
async def test_edit_file_not_found(test_user: User) -> None:
    """edit_file should return error for missing disk file."""
    edit_fn = _get_tool_fn(test_user.id, "edit_file")
    result = await edit_fn(path="MISSING.md", old_text="a", new_text="b")
    assert result.is_error is True
    assert "not found" in result.content.lower()


# --- Tool registration tests ---


def test_workspace_tools_registered(test_user: User) -> None:
    """create_workspace_tools should return read, write, edit, and delete tools."""
    tools = create_workspace_tools(test_user.id)
    names = [t.name for t in tools]
    assert "read_file" in names
    assert "write_file" in names
    assert "edit_file" in names
    assert "delete_file" in names
    assert len(tools) == 4


# --- delete_file tests (always disk-based) ---


@pytest.mark.asyncio()
async def test_delete_file_success(test_user: User) -> None:
    """delete_file should remove the file."""
    cdir = _user_dir(test_user)
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "BOOTSTRAP.md").write_text("bootstrap content\n", encoding="utf-8")

    delete_fn = _get_tool_fn(test_user.id, "delete_file")
    result = await delete_fn(path="BOOTSTRAP.md")
    assert result.is_error is False
    assert "Deleted" in result.content
    assert not (cdir / "BOOTSTRAP.md").exists()


@pytest.mark.asyncio()
async def test_delete_file_not_found(test_user: User) -> None:
    """delete_file should return error for missing file."""
    delete_fn = _get_tool_fn(test_user.id, "delete_file")
    result = await delete_fn(path="NONEXISTENT.md")
    assert result.is_error is True
    assert "not found" in result.content.lower()


@pytest.mark.asyncio()
async def test_delete_file_protected(test_user: User) -> None:
    """delete_file should reject protected files."""
    delete_fn = _get_tool_fn(test_user.id, "delete_file")
    for protected in ("USER.md", "SOUL.md", "HEARTBEAT.md"):
        result = await delete_fn(path=protected)
        assert result.is_error is True
        assert "protected" in result.content.lower()


@pytest.mark.asyncio()
async def test_delete_file_protected_via_path_variant(test_user: User) -> None:
    """delete_file should catch protected files even with path variations like ./USER.md."""
    delete_fn = _get_tool_fn(test_user.id, "delete_file")
    for variant in ("./USER.md", "subdir/../SOUL.md"):
        result = await delete_fn(path=variant)
        assert result.is_error is True
        assert "protected" in result.content.lower()


@pytest.mark.asyncio()
async def test_delete_file_rejects_non_markdown(test_user: User) -> None:
    """delete_file should reject non-markdown files."""
    delete_fn = _get_tool_fn(test_user.id, "delete_file")
    result = await delete_fn(path="user.json")
    assert result.is_error is True
    assert ".md" in result.content


@pytest.mark.asyncio()
async def test_delete_file_rejects_path_traversal(test_user: User) -> None:
    """delete_file should reject paths that escape the user directory."""
    delete_fn = _get_tool_fn(test_user.id, "delete_file")
    result = await delete_fn(path="../../etc/hack.md")
    assert result.is_error is True


# --- DB-backed virtual file tests for SOUL.md and HEARTBEAT.md ---


@pytest.mark.asyncio()
async def test_soul_md_roundtrip(test_user: User) -> None:
    """SOUL.md should read/write through the DB."""
    write_fn = _get_tool_fn(test_user.id, "write_file")
    await write_fn(path="SOUL.md", content="# Soul\n\nDirect and helpful.")
    assert _get_user_column(test_user.id, "soul_text") == "# Soul\n\nDirect and helpful."

    read_fn = _get_tool_fn(test_user.id, "read_file")
    result = await read_fn(path="SOUL.md")
    assert "Direct and helpful" in result.content


@pytest.mark.asyncio()
async def test_heartbeat_md_roundtrip(test_user: User) -> None:
    """HEARTBEAT.md should read/write through the DB."""
    write_fn = _get_tool_fn(test_user.id, "write_file")
    await write_fn(path="HEARTBEAT.md", content="# Heartbeat\n\n- Follow up with Jake")
    assert "Follow up with Jake" in _get_user_column(test_user.id, "heartbeat_text")

    read_fn = _get_tool_fn(test_user.id, "read_file")
    result = await read_fn(path="HEARTBEAT.md")
    assert "Follow up with Jake" in result.content


# --- MemoryDocument-backed virtual file tests (MEMORY.md, HISTORY.md) ---


def _set_memory_doc(user_id: str, column: str, value: str) -> None:
    """Set a column on MemoryDocument directly in the DB."""
    db = _db_module.SessionLocal()
    try:
        doc = db.query(MemoryDocument).filter_by(user_id=user_id).first()
        if doc is None:
            doc = MemoryDocument(user_id=user_id, memory_text="", history_text="")
            db.add(doc)
            db.flush()
        setattr(doc, column, value)
        db.commit()
    finally:
        db.close()


def _get_memory_doc(user_id: str, column: str) -> str:
    """Read a column from MemoryDocument in the DB."""
    db = _db_module.SessionLocal()
    try:
        doc = db.query(MemoryDocument).filter_by(user_id=user_id).first()
        if doc is None:
            return ""
        return getattr(doc, column, "") or ""
    finally:
        db.close()


@pytest.mark.asyncio()
async def test_read_memory_md_via_path(test_user: User) -> None:
    """read_file('memory/MEMORY.md') should read from MemoryDocument.memory_text."""
    _set_memory_doc(test_user.id, "memory_text", "- User prefers morning check-ins\n")

    read_fn = _get_tool_fn(test_user.id, "read_file")
    result = await read_fn(path="memory/MEMORY.md")
    assert result.is_error is False
    assert "morning check-ins" in result.content


@pytest.mark.asyncio()
async def test_read_memory_md_top_level(test_user: User) -> None:
    """read_file('MEMORY.md') should also read from MemoryDocument."""
    _set_memory_doc(test_user.id, "memory_text", "- Has 3 active jobs\n")

    read_fn = _get_tool_fn(test_user.id, "read_file")
    result = await read_fn(path="MEMORY.md")
    assert result.is_error is False
    assert "3 active jobs" in result.content


@pytest.mark.asyncio()
async def test_read_memory_md_empty(test_user: User) -> None:
    """read_file('memory/MEMORY.md') should return '(empty)' when no MemoryDocument exists."""
    read_fn = _get_tool_fn(test_user.id, "read_file")
    result = await read_fn(path="memory/MEMORY.md")
    assert result.is_error is False
    assert result.content == "(empty)"


@pytest.mark.asyncio()
async def test_write_memory_md_via_path(test_user: User) -> None:
    """write_file('memory/MEMORY.md') should write to MemoryDocument.memory_text."""
    write_fn = _get_tool_fn(test_user.id, "write_file")
    result = await write_fn(path="memory/MEMORY.md", content="- Rates: $85/hr\n")
    assert result.is_error is False
    assert "Wrote" in result.content
    assert "Rates: $85/hr" in _get_memory_doc(test_user.id, "memory_text")


@pytest.mark.asyncio()
async def test_write_memory_md_creates_doc(test_user: User) -> None:
    """write_file should create MemoryDocument if it doesn't exist yet."""
    write_fn = _get_tool_fn(test_user.id, "write_file")
    await write_fn(path="MEMORY.md", content="fresh memory")
    assert "fresh memory" in _get_memory_doc(test_user.id, "memory_text")


@pytest.mark.asyncio()
async def test_edit_memory_md(test_user: User) -> None:
    """edit_file should work on memory/MEMORY.md."""
    _set_memory_doc(test_user.id, "memory_text", "- Rate: $85/hr\n- Hours: 8-5\n")

    edit_fn = _get_tool_fn(test_user.id, "edit_file")
    result = await edit_fn(path="memory/MEMORY.md", old_text="$85/hr", new_text="$100/hr")
    assert result.is_error is False
    assert "$100/hr" in _get_memory_doc(test_user.id, "memory_text")


@pytest.mark.asyncio()
async def test_history_md_roundtrip(test_user: User) -> None:
    """HISTORY.md should read/write through MemoryDocument.history_text."""
    _set_memory_doc(test_user.id, "history_text", "2026-03-18: Session compacted\n")

    read_fn = _get_tool_fn(test_user.id, "read_file")
    result = await read_fn(path="memory/HISTORY.md")
    assert result.is_error is False
    assert "Session compacted" in result.content


@pytest.mark.asyncio()
async def test_delete_memory_md_protected(test_user: User) -> None:
    """delete_file should reject MEMORY.md and HISTORY.md as protected files."""
    delete_fn = _get_tool_fn(test_user.id, "delete_file")
    for path in ("memory/MEMORY.md", "memory/HISTORY.md"):
        result = await delete_fn(path=path)
        assert result.is_error is True
        assert "protected" in result.content.lower()
