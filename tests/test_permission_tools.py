"""Tests for the update_permission agent tool."""

import asyncio

from backend.app.agent.approval import PermissionLevel, get_approval_store
from backend.app.agent.tools.names import ToolName
from backend.app.agent.tools.permission_tools import create_permission_tools
from backend.app.agent.tools.registry import (
    ToolContext,
    default_registry,
    ensure_tool_modules_imported,
)
from backend.app.models import User

ensure_tool_modules_imported()


# ---------------------------------------------------------------------------
# Registration tests
# ---------------------------------------------------------------------------


def test_permissions_factory_registered() -> None:
    """The permissions factory is registered as a core factory."""
    assert "permissions" in default_registry.factory_names
    assert "permissions" in default_registry.core_factory_names


def test_permissions_sub_tools_metadata() -> None:
    """The permissions factory exposes correct sub-tool metadata."""
    sub_tools = default_registry.get_factory_sub_tools("permissions")
    assert len(sub_tools) == 1
    st = sub_tools[0]
    assert st.name == ToolName.UPDATE_PERMISSION
    assert st.default_permission == "auto"


def test_permissions_not_specialist() -> None:
    """Permissions factory should not appear in specialist summaries."""
    assert "permissions" not in default_registry.specialist_factory_names


# ---------------------------------------------------------------------------
# Tool execution tests
# ---------------------------------------------------------------------------


def test_update_permission_sets_auto() -> None:
    """update_permission with 'auto' stores AUTO level."""
    user_id = "test-perm-user-1"
    tools = create_permission_tools(user_id)
    assert len(tools) == 1
    tool = tools[0]
    assert tool.name == ToolName.UPDATE_PERMISSION

    result = asyncio.get_event_loop().run_until_complete(tool.function("send_reply", "auto"))
    assert not result.is_error
    assert "run freely" in result.content

    store = get_approval_store()
    level = store.check_permission(user_id, "send_reply")
    assert level == PermissionLevel.AUTO


def test_update_permission_sets_ask() -> None:
    """update_permission with 'ask' stores ASK level."""
    user_id = "test-perm-user-2"
    tools = create_permission_tools(user_id)
    tool = tools[0]

    result = asyncio.get_event_loop().run_until_complete(
        tool.function("calendar_create_event", "ask")
    )
    assert not result.is_error
    assert "ask first" in result.content

    store = get_approval_store()
    level = store.check_permission(user_id, "calendar_create_event")
    assert level == PermissionLevel.ASK


def test_update_permission_sets_deny() -> None:
    """update_permission with 'deny' stores DENY level."""
    user_id = "test-perm-user-3"
    tools = create_permission_tools(user_id)
    tool = tools[0]

    result = asyncio.get_event_loop().run_until_complete(tool.function("qb_create", "deny"))
    assert not result.is_error
    assert "blocked" in result.content

    store = get_approval_store()
    level = store.check_permission(user_id, "qb_create")
    assert level == PermissionLevel.DENY


def test_update_permission_aliases() -> None:
    """Human-friendly aliases ('always', 'never', 'block') map correctly."""
    user_id = "test-perm-user-4"
    tools = create_permission_tools(user_id)
    tool = tools[0]
    store = get_approval_store()
    loop = asyncio.get_event_loop()

    # "always" -> auto
    result = loop.run_until_complete(tool.function("send_reply", "always"))
    assert not result.is_error
    assert store.check_permission(user_id, "send_reply") == PermissionLevel.AUTO

    # "never" -> deny
    result = loop.run_until_complete(tool.function("send_reply", "never"))
    assert not result.is_error
    assert store.check_permission(user_id, "send_reply") == PermissionLevel.DENY

    # "block" -> deny
    result = loop.run_until_complete(tool.function("send_reply", "block"))
    assert not result.is_error
    assert store.check_permission(user_id, "send_reply") == PermissionLevel.DENY

    # "allow" -> auto
    result = loop.run_until_complete(tool.function("send_reply", "allow"))
    assert not result.is_error
    assert store.check_permission(user_id, "send_reply") == PermissionLevel.AUTO


def test_update_permission_invalid_level() -> None:
    """update_permission rejects unrecognized permission levels."""
    user_id = "test-perm-user-5"
    tools = create_permission_tools(user_id)
    tool = tools[0]

    result = asyncio.get_event_loop().run_until_complete(tool.function("send_reply", "yolo"))
    assert result.is_error
    assert "Unknown permission" in result.content


def test_update_permission_case_insensitive() -> None:
    """Permission values are case-insensitive."""
    user_id = "test-perm-user-6"
    tools = create_permission_tools(user_id)
    tool = tools[0]
    store = get_approval_store()
    loop = asyncio.get_event_loop()

    result = loop.run_until_complete(tool.function("send_reply", "AUTO"))
    assert not result.is_error

    result = loop.run_until_complete(tool.function("send_reply", "  Ask  "))
    assert not result.is_error
    assert store.check_permission(user_id, "send_reply") == PermissionLevel.ASK


def test_core_tools_include_update_permission() -> None:
    """update_permission appears in core tools when created from context."""
    user = User(id="test-core-perm", user_id="test")
    ctx = ToolContext(user=user)
    core_tools = default_registry.create_core_tools(ctx)
    names = {t.name for t in core_tools}
    assert ToolName.UPDATE_PERMISSION in names
