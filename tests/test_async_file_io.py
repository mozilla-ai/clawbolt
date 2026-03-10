"""Regression tests for asyncio.to_thread wrapping of blocking file I/O.

Verifies that blocking file operations in async code paths are delegated
to a thread via asyncio.to_thread() rather than blocking the event loop.

These tests inspect the source code to verify asyncio.to_thread is used
at the correct locations, and run functional tests to ensure correctness.

Fixes #553.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.app.agent.file_store import (
    FileSessionStore,
    HeartbeatStore,
    UserData,
)
from backend.app.agent.tools.workspace_tools import create_workspace_tools
from backend.app.config import settings

# ---------------------------------------------------------------------------
# Source-level checks: verify asyncio.to_thread is used in the right places
# ---------------------------------------------------------------------------


def _source_of(module_path: str) -> str:
    """Read source code of a module relative to the project root."""
    path = Path(__file__).parent.parent / module_path
    return path.read_text(encoding="utf-8")


def test_estimates_router_uses_to_thread_for_read_bytes() -> None:
    """estimates.py should use asyncio.to_thread for pdf_path.read_bytes."""
    source = _source_of("backend/app/routers/estimates.py")
    assert "asyncio.to_thread" in source, "estimates.py must use asyncio.to_thread"
    assert "pdf_path.read_bytes()" not in source, (
        "estimates.py should not call pdf_path.read_bytes() directly"
    )


def test_estimate_tools_uses_to_thread_for_write_bytes() -> None:
    """estimate_tools.py should use asyncio.to_thread for pdf_path.write_bytes."""
    source = _source_of("backend/app/agent/tools/estimate_tools.py")
    assert "asyncio.to_thread" in source, "estimate_tools.py must use asyncio.to_thread"
    assert "pdf_path.write_bytes(pdf_bytes)" not in source, (
        "estimate_tools.py should not call pdf_path.write_bytes() directly"
    )


def test_telegram_uses_to_thread_for_read_bytes() -> None:
    """telegram.py should use asyncio.to_thread for local_path.read_bytes."""
    source = _source_of("backend/app/channels/telegram.py")
    assert "asyncio.to_thread(local_path.read_bytes)" in source, (
        "telegram.py must use asyncio.to_thread for local_path.read_bytes"
    )
    assert "local_path.read_bytes()" not in source, (
        "telegram.py should not call local_path.read_bytes() directly"
    )


def test_file_store_add_message_uses_to_thread() -> None:
    """FileSessionStore.add_message should use asyncio.to_thread for _append_jsonl."""
    source = _source_of("backend/app/agent/file_store.py")
    assert (
        "asyncio.to_thread(\n                _append_jsonl" in source
        or "asyncio.to_thread(_append_jsonl" in source
    ), "add_message should use asyncio.to_thread for _append_jsonl"


def test_file_store_log_heartbeat_uses_to_thread() -> None:
    """HeartbeatStore.log_heartbeat should use asyncio.to_thread for _append_jsonl."""
    source = _source_of("backend/app/agent/file_store.py")
    assert "await asyncio.to_thread(_append_jsonl, self._log_path" in source, (
        "log_heartbeat should use asyncio.to_thread for _append_jsonl"
    )


def test_workspace_tools_use_to_thread() -> None:
    """workspace_tools.py should use asyncio.to_thread for file I/O."""
    source = _source_of("backend/app/agent/tools/workspace_tools.py")
    assert "asyncio.to_thread(resolved.read_text" in source, (
        "workspace_tools.py should use asyncio.to_thread for read_text"
    )
    assert "asyncio.to_thread(resolved.write_text" in source, (
        "workspace_tools.py should use asyncio.to_thread for write_text"
    )


# ---------------------------------------------------------------------------
# Functional tests: verify the changes do not break existing behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_session_store_add_message_still_works(
    test_user: UserData,
) -> None:
    """FileSessionStore.add_message should still append messages correctly."""
    store = FileSessionStore(test_user.id)
    session, _ = await store.get_or_create_session()
    msg = await store.add_message(session, direction="inbound", body="Hello from test")
    assert msg.body == "Hello from test"
    assert msg.direction == "inbound"
    assert len(session.messages) == 1


@pytest.mark.asyncio()
async def test_heartbeat_log_still_works(
    test_user: UserData,
) -> None:
    """HeartbeatStore.log_heartbeat should still log entries correctly."""
    store = HeartbeatStore(test_user.id)
    await store.log_heartbeat()
    count = await store.get_daily_count()
    assert count == 1


@pytest.mark.asyncio()
async def test_workspace_read_file_still_works(
    test_user: UserData,
) -> None:
    """Workspace read_file should still read files correctly."""
    cdir = Path(settings.data_dir) / str(test_user.id)
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "USER.md").write_text("# User\n\n- Name: Jake\n", encoding="utf-8")

    tools = create_workspace_tools(test_user.id)
    read_fn = next(t.function for t in tools if t.name == "read_file")
    result = await read_fn(path="USER.md")
    assert result.is_error is False
    assert "Jake" in result.content


@pytest.mark.asyncio()
async def test_workspace_write_file_still_works(
    test_user: UserData,
) -> None:
    """Workspace write_file should still write files correctly."""
    cdir = Path(settings.data_dir) / str(test_user.id)
    cdir.mkdir(parents=True, exist_ok=True)

    tools = create_workspace_tools(test_user.id)
    write_fn = next(t.function for t in tools if t.name == "write_file")
    result = await write_fn(path="USER.md", content="# User\n\n- Name: Sarah\n")
    assert result.is_error is False
    written = (cdir / "USER.md").read_text(encoding="utf-8")
    assert "Sarah" in written


@pytest.mark.asyncio()
async def test_workspace_edit_file_still_works(
    test_user: UserData,
) -> None:
    """Workspace edit_file should still edit files correctly."""
    cdir = Path(settings.data_dir) / str(test_user.id)
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "USER.md").write_text("- Rate: $85/hr\n", encoding="utf-8")

    tools = create_workspace_tools(test_user.id)
    edit_fn = next(t.function for t in tools if t.name == "edit_file")
    result = await edit_fn(path="USER.md", old_text="$85/hr", new_text="$100/hr")
    assert result.is_error is False
    assert "$100/hr" in (cdir / "USER.md").read_text(encoding="utf-8")
