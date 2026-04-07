"""Tests for permissions via workspace tools (replaces old update_permission tool tests)."""

import asyncio
import json

import pytest

from backend.app.agent.approval import get_approval_store
from backend.app.agent.tools.names import ToolName
from backend.app.agent.tools.registry import (
    ToolContext,
    default_registry,
    ensure_tool_modules_imported,
)
from backend.app.agent.tools.workspace_tools import create_workspace_tools
from backend.app.models import User

ensure_tool_modules_imported()


# ---------------------------------------------------------------------------
# Registration tests: permissions factory is removed
# ---------------------------------------------------------------------------


def test_permissions_factory_not_registered() -> None:
    """The permissions factory should no longer be in the registry."""
    assert "permissions" not in default_registry.factory_names


@pytest.mark.asyncio()
async def test_no_update_permission_in_core_tools() -> None:
    """update_permission tool should no longer appear in core tools."""
    user = User(id="test-core-perm", user_id="test")
    ctx = ToolContext(user=user)
    core_tools = await default_registry.create_core_tools(ctx)
    names = {t.name for t in core_tools}
    assert "update_permission" not in names


# ---------------------------------------------------------------------------
# PERMISSIONS.json via workspace tools
# ---------------------------------------------------------------------------


def test_read_permissions_json(tmp_path: object) -> None:
    """read_file("PERMISSIONS.json") returns the permissions content."""
    user_id = "test-ws-perm-read"
    # Create the PERMISSIONS.json file first
    store = get_approval_store()
    store.ensure_complete(user_id)

    tools = create_workspace_tools(user_id)
    read_tool = next(t for t in tools if t.name == ToolName.READ_FILE)

    result = asyncio.get_event_loop().run_until_complete(read_tool.function("PERMISSIONS.json"))
    assert not result.is_error
    data = json.loads(result.content)
    assert "tools" in data
    assert "version" in data


def test_edit_permissions_json(tmp_path: object) -> None:
    """edit_file can change a permission level in PERMISSIONS.json."""
    user_id = "test-ws-perm-edit"
    store = get_approval_store()
    store.ensure_complete(user_id)

    tools = create_workspace_tools(user_id)
    read_tool = next(t for t in tools if t.name == ToolName.READ_FILE)
    edit_tool = next(t for t in tools if t.name == ToolName.EDIT_FILE)

    # Read current content to find the send_reply entry
    result = asyncio.get_event_loop().run_until_complete(read_tool.function("PERMISSIONS.json"))
    assert not result.is_error
    assert '"send_reply": "ask"' in result.content

    # Edit send_reply from ask to always
    result = asyncio.get_event_loop().run_until_complete(
        edit_tool.function("PERMISSIONS.json", '"send_reply": "ask"', '"send_reply": "always"')
    )
    assert not result.is_error
    assert "Updated" in result.content

    # Verify the change
    result = asyncio.get_event_loop().run_until_complete(read_tool.function("PERMISSIONS.json"))
    data = json.loads(result.content)
    assert data["tools"]["send_reply"] == "always"


def test_write_permissions_json(tmp_path: object) -> None:
    """write_file can overwrite PERMISSIONS.json."""
    user_id = "test-ws-perm-write"
    store = get_approval_store()
    store.ensure_complete(user_id)

    tools = create_workspace_tools(user_id)
    write_tool = next(t for t in tools if t.name == ToolName.WRITE_FILE)
    read_tool = next(t for t in tools if t.name == ToolName.READ_FILE)

    new_content = json.dumps({"version": 1, "tools": {"send_reply": "deny"}, "resources": {}})
    result = asyncio.get_event_loop().run_until_complete(
        write_tool.function("PERMISSIONS.json", new_content)
    )
    assert not result.is_error

    result = asyncio.get_event_loop().run_until_complete(read_tool.function("PERMISSIONS.json"))
    data = json.loads(result.content)
    assert data["tools"]["send_reply"] == "deny"


def test_delete_permissions_json_blocked(tmp_path: object) -> None:
    """PERMISSIONS.json cannot be deleted (protected file)."""
    user_id = "test-ws-perm-delete"
    store = get_approval_store()
    store.ensure_complete(user_id)

    tools = create_workspace_tools(user_id)
    delete_tool = next(t for t in tools if t.name == ToolName.DELETE_FILE)

    result = asyncio.get_event_loop().run_until_complete(delete_tool.function("PERMISSIONS.json"))
    assert result.is_error
    assert "protected" in result.content.lower()
