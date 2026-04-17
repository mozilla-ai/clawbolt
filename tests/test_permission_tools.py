"""Tests for permissions via workspace tools (replaces old update_permission tool tests)."""

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


async def test_read_permissions_json(tmp_path: object) -> None:
    """read_file("PERMISSIONS.json") returns the permissions content."""
    user_id = "test-ws-perm-read"
    # Create the PERMISSIONS.json file first
    store = get_approval_store()
    store.ensure_complete(user_id)

    tools = create_workspace_tools(user_id)
    read_tool = next(t for t in tools if t.name == ToolName.READ_FILE)

    result = await read_tool.function("PERMISSIONS.json")
    assert not result.is_error
    data = json.loads(result.content)
    assert "tools" in data
    assert "version" in data


async def test_edit_permissions_json(tmp_path: object) -> None:
    """edit_file on PERMISSIONS.json flows through ApprovalStore's DB row."""
    user_id = "test-ws-perm-edit"
    store = get_approval_store()
    store.ensure_complete(user_id)

    tools = create_workspace_tools(user_id)
    read_tool = next(t for t in tools if t.name == ToolName.READ_FILE)
    edit_tool = next(t for t in tools if t.name == ToolName.EDIT_FILE)

    # Sanity: read returns the default-seeded send_media_reply level.
    result = await read_tool.function("PERMISSIONS.json")
    assert not result.is_error
    # send_media_reply defaults to always since the messaging-tools flip.
    original = json.loads(result.content)
    assert original["tools"]["send_media_reply"] == "always"

    # Flip send_media_reply from always to deny.
    result = await edit_tool.function(
        "PERMISSIONS.json", '"send_media_reply": "always"', '"send_media_reply": "deny"'
    )
    assert not result.is_error
    assert "Updated" in result.content

    result = await read_tool.function("PERMISSIONS.json")
    data = json.loads(result.content)
    assert data["tools"]["send_media_reply"] == "deny"


async def test_write_permissions_json(tmp_path: object) -> None:
    """write_file can overwrite PERMISSIONS.json; content is normalized."""
    user_id = "test-ws-perm-write"
    store = get_approval_store()
    store.ensure_complete(user_id)

    tools = create_workspace_tools(user_id)
    write_tool = next(t for t in tools if t.name == ToolName.WRITE_FILE)
    read_tool = next(t for t in tools if t.name == ToolName.READ_FILE)

    # Minified input must be stored as indented JSON so later edit_file
    # calls have a stable shape to match against.
    minified = '{"version": 1, "tools": {"send_media_reply": "deny"}, "resources": {}}'
    result = await write_tool.function("PERMISSIONS.json", minified)
    assert not result.is_error

    result = await read_tool.function("PERMISSIONS.json")
    data = json.loads(result.content)
    assert data["tools"]["send_media_reply"] == "deny"
    # Indented: newlines plus a 2-space prefix on nested keys.
    assert "\n" in result.content
    assert '  "tools"' in result.content


async def test_delete_permissions_json_blocked(tmp_path: object) -> None:
    """PERMISSIONS.json cannot be deleted (protected file)."""
    user_id = "test-ws-perm-delete"
    store = get_approval_store()
    store.ensure_complete(user_id)

    tools = create_workspace_tools(user_id)
    delete_tool = next(t for t in tools if t.name == ToolName.DELETE_FILE)

    result = await delete_tool.function("PERMISSIONS.json")
    assert result.is_error
    assert "protected" in result.content.lower()
