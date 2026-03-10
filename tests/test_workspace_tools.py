"""Tests for generic workspace file tools (read_file, write_file, edit_file)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from backend.app.agent.file_store import UserData
from backend.app.agent.tools.base import ToolResult
from backend.app.agent.tools.workspace_tools import create_workspace_tools
from backend.app.config import settings


def _get_tool_fn(user_id: int, tool_name: str) -> Callable[..., Awaitable[ToolResult]]:
    """Return the async function for the named tool."""
    tools = create_workspace_tools(user_id)
    for t in tools:
        if t.name == tool_name:
            return t.function
    msg = f"Tool {tool_name!r} not found"
    raise ValueError(msg)


def _user_dir(user: UserData) -> Path:
    return Path(settings.data_dir) / str(user.id)


# --- read_file tests ---


@pytest.mark.asyncio()
async def test_read_file_success(test_user: UserData) -> None:
    """read_file should return file contents."""
    cdir = _user_dir(test_user)
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "USER.md").write_text("# User\n\n- Name: Jake\n", encoding="utf-8")

    read_fn = _get_tool_fn(test_user.id, "read_file")
    result = await read_fn(path="USER.md")
    assert result.is_error is False
    assert "Jake" in result.content


@pytest.mark.asyncio()
async def test_read_file_not_found(test_user: UserData) -> None:
    """read_file should return error for missing file."""
    read_fn = _get_tool_fn(test_user.id, "read_file")
    result = await read_fn(path="NONEXISTENT.md")
    assert result.is_error is True
    assert "not found" in result.content.lower()


@pytest.mark.asyncio()
async def test_read_file_rejects_non_markdown(test_user: UserData) -> None:
    """read_file should reject non-markdown files."""
    read_fn = _get_tool_fn(test_user.id, "read_file")
    result = await read_fn(path="user.json")
    assert result.is_error is True
    assert ".md" in result.content


@pytest.mark.asyncio()
async def test_read_file_rejects_path_traversal(test_user: UserData) -> None:
    """read_file should reject paths that escape the user directory."""
    read_fn = _get_tool_fn(test_user.id, "read_file")
    result = await read_fn(path="../../etc/passwd.md")
    assert result.is_error is True


# --- write_file tests ---


@pytest.mark.asyncio()
async def test_write_file_creates_new(test_user: UserData) -> None:
    """write_file should create a new file."""
    cdir = _user_dir(test_user)
    cdir.mkdir(parents=True, exist_ok=True)

    write_fn = _get_tool_fn(test_user.id, "write_file")
    result = await write_fn(path="USER.md", content="# User\n\n- Name: Sarah\n")
    assert result.is_error is False
    assert "Wrote" in result.content
    assert (cdir / "USER.md").read_text(encoding="utf-8") == "# User\n\n- Name: Sarah\n"


@pytest.mark.asyncio()
async def test_write_file_overwrites(test_user: UserData) -> None:
    """write_file should overwrite existing file."""
    cdir = _user_dir(test_user)
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "USER.md").write_text("old content", encoding="utf-8")

    write_fn = _get_tool_fn(test_user.id, "write_file")
    await write_fn(path="USER.md", content="new content")
    assert (cdir / "USER.md").read_text(encoding="utf-8") == "new content"


@pytest.mark.asyncio()
async def test_write_file_creates_subdirectory(test_user: UserData) -> None:
    """write_file should create parent directories."""
    cdir = _user_dir(test_user)
    cdir.mkdir(parents=True, exist_ok=True)

    write_fn = _get_tool_fn(test_user.id, "write_file")
    result = await write_fn(path="memory/NOTES.md", content="# Notes\n")
    assert result.is_error is False
    assert (cdir / "memory" / "NOTES.md").exists()


@pytest.mark.asyncio()
async def test_write_file_rejects_non_markdown(test_user: UserData) -> None:
    """write_file should reject non-markdown files."""
    write_fn = _get_tool_fn(test_user.id, "write_file")
    result = await write_fn(path="evil.json", content="{}")
    assert result.is_error is True


@pytest.mark.asyncio()
async def test_write_file_rejects_path_traversal(test_user: UserData) -> None:
    """write_file should reject paths that escape the user directory."""
    write_fn = _get_tool_fn(test_user.id, "write_file")
    result = await write_fn(path="../../../tmp/hack.md", content="nope")
    assert result.is_error is True


# --- edit_file tests ---


@pytest.mark.asyncio()
async def test_edit_file_replaces_text(test_user: UserData) -> None:
    """edit_file should replace exact text."""
    cdir = _user_dir(test_user)
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "USER.md").write_text("- Rate: $85/hr\n- Hours: 8-5\n", encoding="utf-8")

    edit_fn = _get_tool_fn(test_user.id, "edit_file")
    result = await edit_fn(path="USER.md", old_text="$85/hr", new_text="$100/hr")
    assert result.is_error is False
    assert (cdir / "USER.md").read_text(encoding="utf-8") == "- Rate: $100/hr\n- Hours: 8-5\n"


@pytest.mark.asyncio()
async def test_edit_file_text_not_found(test_user: UserData) -> None:
    """edit_file should return error when old_text not found."""
    cdir = _user_dir(test_user)
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "USER.md").write_text("- Name: Jake\n", encoding="utf-8")

    edit_fn = _get_tool_fn(test_user.id, "edit_file")
    result = await edit_fn(path="USER.md", old_text="nonexistent text", new_text="replacement")
    assert result.is_error is True
    assert "not found" in result.content.lower()


@pytest.mark.asyncio()
async def test_edit_file_ambiguous_match(test_user: UserData) -> None:
    """edit_file should return error when old_text matches multiple times."""
    cdir = _user_dir(test_user)
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "USER.md").write_text("foo bar\nfoo baz\n", encoding="utf-8")

    edit_fn = _get_tool_fn(test_user.id, "edit_file")
    result = await edit_fn(path="USER.md", old_text="foo", new_text="qux")
    assert result.is_error is True
    assert "2 matches" in result.content


@pytest.mark.asyncio()
async def test_edit_file_not_found(test_user: UserData) -> None:
    """edit_file should return error for missing file."""
    edit_fn = _get_tool_fn(test_user.id, "edit_file")
    result = await edit_fn(path="MISSING.md", old_text="a", new_text="b")
    assert result.is_error is True
    assert "not found" in result.content.lower()


# --- Tool registration tests ---


def test_workspace_tools_registered(test_user: UserData) -> None:
    """create_workspace_tools should return read, write, edit, and delete tools."""
    tools = create_workspace_tools(test_user.id)
    names = [t.name for t in tools]
    assert "read_file" in names
    assert "write_file" in names
    assert "edit_file" in names
    assert "delete_file" in names
    assert len(tools) == 4


# --- delete_file tests ---


@pytest.mark.asyncio()
async def test_delete_file_success(test_user: UserData) -> None:
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
async def test_delete_file_not_found(test_user: UserData) -> None:
    """delete_file should return error for missing file."""
    delete_fn = _get_tool_fn(test_user.id, "delete_file")
    result = await delete_fn(path="NONEXISTENT.md")
    assert result.is_error is True
    assert "not found" in result.content.lower()


@pytest.mark.asyncio()
async def test_delete_file_protected(test_user: UserData) -> None:
    """delete_file should reject protected files."""
    delete_fn = _get_tool_fn(test_user.id, "delete_file")
    for protected in ("USER.md", "SOUL.md", "HEARTBEAT.md"):
        result = await delete_fn(path=protected)
        assert result.is_error is True
        assert "protected" in result.content.lower()


@pytest.mark.asyncio()
async def test_delete_file_rejects_non_markdown(test_user: UserData) -> None:
    """delete_file should reject non-markdown files."""
    delete_fn = _get_tool_fn(test_user.id, "delete_file")
    result = await delete_fn(path="user.json")
    assert result.is_error is True
    assert ".md" in result.content


@pytest.mark.asyncio()
async def test_delete_file_rejects_path_traversal(test_user: UserData) -> None:
    """delete_file should reject paths that escape the user directory."""
    delete_fn = _get_tool_fn(test_user.id, "delete_file")
    result = await delete_fn(path="../../etc/hack.md")
    assert result.is_error is True
