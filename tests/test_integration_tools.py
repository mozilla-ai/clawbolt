"""Tests for the manage_integration chat tool."""

from unittest.mock import patch

import pytest

from backend.app.agent.stores import ToolConfigStore
from backend.app.agent.tools.base import ToolResult
from backend.app.agent.tools.integration_tools import create_integration_tools
from backend.app.agent.tools.names import ToolName
from backend.app.agent.tools.registry import (
    ToolContext,
    default_registry,
    ensure_tool_modules_imported,
)
from backend.app.models import User
from backend.app.services.oauth import OAuthConfig

ensure_tool_modules_imported()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _call(user: User, action: str, target: str | None = None) -> ToolResult:
    """Create tools and call manage_integration with the given args."""
    ctx = ToolContext(user=user)
    tools = create_integration_tools(ctx)
    tool = next(t for t in tools if t.name == ToolName.MANAGE_INTEGRATION)
    if target is not None:
        return await tool.function(action=action, target=target)
    return await tool.function(action=action)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_integration_factory_registered() -> None:
    """The integration factory should be registered as a core factory."""
    assert "integration" in default_registry.factory_names
    assert "integration" in default_registry.core_factory_names


@pytest.mark.asyncio()
async def test_manage_integration_in_core_tools() -> None:
    """manage_integration should appear in core tools."""
    user = User(id="test-core-int", user_id="test")
    ctx = ToolContext(user=user)
    core_tools = await default_registry.create_core_tools(ctx)
    names = {t.name for t in core_tools}
    assert ToolName.MANAGE_INTEGRATION in names


# ---------------------------------------------------------------------------
# Status action
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_status_shows_all_groups(test_user: User) -> None:
    """Status should list all registered tool groups."""
    result = await _call(test_user, "status")
    assert not result.is_error
    # Should contain at least some known groups
    assert "workspace" in result.content
    assert "Core tools:" in result.content


@pytest.mark.asyncio()
async def test_status_shows_enabled_disabled(test_user: User) -> None:
    """Status should reflect disabled groups."""
    store = ToolConfigStore(test_user.id)
    await store.set_enabled("calendar", enabled=False)

    result = await _call(test_user, "status")
    assert not result.is_error
    assert "disabled" in result.content


@pytest.mark.asyncio()
async def test_usage_hint_instructs_status_check_first(test_user: User) -> None:
    """The manage_integration usage_hint must tell the agent to call
    action='status' before offering connect links.

    Regression for #1037: the agent re-prompted for already-connected
    integrations because the rule lived only in bootstrap.md, which
    gets deleted at the end of onboarding. The durable home is the
    tool's usage_hint.
    """
    ctx = ToolContext(user=test_user)
    tools = create_integration_tools(ctx)
    tool = next(t for t in tools if t.name == ToolName.MANAGE_INTEGRATION)
    assert tool.usage_hint is not None
    hint = tool.usage_hint.lower()
    assert "status" in hint
    assert "connect" in hint
    assert "before offering" in hint or "first" in hint or "skip" in hint, (
        "usage_hint should instruct status check before offering a connect link"
    )


@pytest.mark.asyncio()
async def test_status_shows_oauth_connection_state(test_user: User) -> None:
    """Status should show connected/not connected for OAuth integrations."""
    mock_config = OAuthConfig(
        integration="google_calendar",
        client_id="test-id",
        client_secret="test-secret",
        authorize_url="https://accounts.google.com/o/oauth2/auth",
        token_url="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/calendar"],
    )
    with (
        patch(
            "backend.app.agent.tools.integration_tools.get_oauth_config",
            return_value=mock_config,
        ),
        patch("backend.app.agent.tools.integration_tools.oauth_service") as mock_oauth,
    ):
        mock_oauth.is_connected.return_value = False
        result = await _call(test_user, "status")
        assert not result.is_error
        assert "not connected" in result.content


# ---------------------------------------------------------------------------
# Enable action
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_enable_domain_tool(test_user: User) -> None:
    """Enabling a domain tool should persist to the store."""
    store = ToolConfigStore(test_user.id)
    await store.set_enabled("calendar", enabled=False)

    # Verify it's disabled
    disabled = await store.get_disabled_tool_names()
    assert "calendar" in disabled

    result = await _call(test_user, "enable", "calendar")
    assert not result.is_error
    assert "Enabled" in result.content

    disabled = await store.get_disabled_tool_names()
    assert "calendar" not in disabled


@pytest.mark.asyncio()
async def test_enable_core_tool_noop(test_user: User) -> None:
    """Enabling a core tool should return a message (it's always enabled)."""
    result = await _call(test_user, "enable", "workspace")
    assert not result.is_error
    assert "always enabled" in result.content


@pytest.mark.asyncio()
async def test_enable_unknown_tool_rejected(test_user: User) -> None:
    """Enabling an unknown tool should return an error."""
    result = await _call(test_user, "enable", "foobar")
    assert result.is_error
    assert "Unknown tool group" in result.content
    assert "foobar" in result.content


# ---------------------------------------------------------------------------
# Disable action
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_disable_domain_tool(test_user: User) -> None:
    """Disabling a domain tool should persist to the store."""
    store = ToolConfigStore(test_user.id)

    result = await _call(test_user, "disable", "calendar")
    assert not result.is_error
    assert "Disabled" in result.content

    disabled = await store.get_disabled_tool_names()
    assert "calendar" in disabled


@pytest.mark.asyncio()
async def test_disable_core_tool_rejected(test_user: User) -> None:
    """Disabling a core tool should return an error."""
    result = await _call(test_user, "disable", "workspace")
    assert result.is_error
    assert "cannot be disabled" in result.content


@pytest.mark.asyncio()
async def test_disable_unknown_tool_rejected(test_user: User) -> None:
    """Disabling an unknown tool should return an error."""
    result = await _call(test_user, "disable", "foobar")
    assert result.is_error
    assert "Unknown tool group" in result.content


# ---------------------------------------------------------------------------
# Connect action
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_connect_returns_oauth_url(test_user: User) -> None:
    """Connecting should return an OAuth URL."""
    mock_config = OAuthConfig(
        integration="google_calendar",
        client_id="test-id",
        client_secret="test-secret",
        authorize_url="https://accounts.google.com/o/oauth2/auth",
        token_url="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/calendar"],
    )
    with (
        patch(
            "backend.app.agent.tools.integration_tools.get_oauth_config",
            return_value=mock_config,
        ),
        patch("backend.app.agent.tools.integration_tools.oauth_service") as mock_oauth,
    ):
        mock_oauth.is_connected.return_value = False
        mock_oauth.get_authorization_url.return_value = (
            "https://accounts.google.com/o/oauth2/auth?client_id=test"
        )

        result = await _call(test_user, "connect", "google_calendar")
        assert not result.is_error
        assert "https://accounts.google.com" in result.content
        mock_oauth.get_authorization_url.assert_called_once_with(
            mock_config, test_user.id, source="chat"
        )


@pytest.mark.asyncio()
async def test_connect_via_tool_group_name(test_user: User) -> None:
    """Connecting with tool group name 'calendar' should map to 'google_calendar'."""
    mock_config = OAuthConfig(
        integration="google_calendar",
        client_id="test-id",
        client_secret="test-secret",
        authorize_url="https://accounts.google.com/o/oauth2/auth",
        token_url="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/calendar"],
    )
    with (
        patch(
            "backend.app.agent.tools.integration_tools.get_oauth_config",
            return_value=mock_config,
        ),
        patch("backend.app.agent.tools.integration_tools.oauth_service") as mock_oauth,
    ):
        mock_oauth.is_connected.return_value = False
        mock_oauth.get_authorization_url.return_value = "https://example.com/auth"

        result = await _call(test_user, "connect", "calendar")
        assert not result.is_error
        assert "https://example.com/auth" in result.content


@pytest.mark.asyncio()
async def test_connect_unconfigured_integration(test_user: User) -> None:
    """Connecting a not-configured integration should return an error."""
    with patch(
        "backend.app.agent.tools.integration_tools.get_oauth_config",
        return_value=None,
    ):
        result = await _call(test_user, "connect", "google_calendar")
        assert result.is_error
        assert "not configured" in result.content


@pytest.mark.asyncio()
async def test_connect_non_oauth_integration(test_user: User) -> None:
    """Connecting a non-OAuth integration should return an error."""
    result = await _call(test_user, "connect", "supplier_pricing")
    assert result.is_error
    assert "does not use OAuth" in result.content


@pytest.mark.asyncio()
async def test_connect_already_connected(test_user: User) -> None:
    """Connecting an already-connected integration should inform the user."""
    mock_config = OAuthConfig(
        integration="google_calendar",
        client_id="test-id",
        client_secret="test-secret",
        authorize_url="https://accounts.google.com/o/oauth2/auth",
        token_url="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/calendar"],
    )
    with (
        patch(
            "backend.app.agent.tools.integration_tools.get_oauth_config",
            return_value=mock_config,
        ),
        patch("backend.app.agent.tools.integration_tools.oauth_service") as mock_oauth,
    ):
        mock_oauth.is_connected.return_value = True

        result = await _call(test_user, "connect", "google_calendar")
        assert not result.is_error
        assert "already connected" in result.content


# ---------------------------------------------------------------------------
# Disconnect action
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_disconnect_removes_tokens(test_user: User) -> None:
    """Disconnecting should call delete_token on the OAuth service."""
    with patch("backend.app.agent.tools.integration_tools.oauth_service") as mock_oauth:
        mock_oauth.is_connected.return_value = True
        mock_oauth.delete_token.return_value = True

        result = await _call(test_user, "disconnect", "google_calendar")
        assert not result.is_error
        assert "Disconnected" in result.content
        mock_oauth.delete_token.assert_called_once_with(test_user.id, "google_calendar")


@pytest.mark.asyncio()
async def test_disconnect_not_connected(test_user: User) -> None:
    """Disconnecting a not-connected integration should return an error."""
    with patch("backend.app.agent.tools.integration_tools.oauth_service") as mock_oauth:
        mock_oauth.is_connected.return_value = False

        result = await _call(test_user, "disconnect", "google_calendar")
        assert result.is_error
        assert "not currently connected" in result.content


@pytest.mark.asyncio()
async def test_disconnect_non_oauth(test_user: User) -> None:
    """Disconnecting a non-OAuth integration should return an error."""
    result = await _call(test_user, "disconnect", "supplier_pricing")
    assert result.is_error
    assert "does not use OAuth" in result.content


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_invalid_action(test_user: User) -> None:
    """An unknown action should return an error."""
    result = await _call(test_user, "foobar", "calendar")
    assert result.is_error
    assert "Unknown action" in result.content


@pytest.mark.asyncio()
async def test_missing_target_for_enable(test_user: User) -> None:
    """Enable without a target should return an error."""
    result = await _call(test_user, "enable")
    assert result.is_error
    assert "requires a target" in result.content


# ---------------------------------------------------------------------------
# ToolConfigStore.set_enabled unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_set_enabled_creates_new_row(test_user: User) -> None:
    """set_enabled should create a row when none exists."""
    store = ToolConfigStore(test_user.id)

    # Initially no disabled tools
    disabled = await store.get_disabled_tool_names()
    assert "calendar" not in disabled

    await store.set_enabled("calendar", enabled=False)
    disabled = await store.get_disabled_tool_names()
    assert "calendar" in disabled


@pytest.mark.asyncio()
async def test_set_enabled_updates_existing_row(test_user: User) -> None:
    """set_enabled should update an existing row."""
    store = ToolConfigStore(test_user.id)

    await store.set_enabled("calendar", enabled=False)
    disabled = await store.get_disabled_tool_names()
    assert "calendar" in disabled

    await store.set_enabled("calendar", enabled=True)
    disabled = await store.get_disabled_tool_names()
    assert "calendar" not in disabled
