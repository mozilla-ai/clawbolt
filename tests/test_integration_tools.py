"""Tests for the manage_integration chat tool."""

from unittest.mock import AsyncMock, patch

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
async def test_usage_hint_lists_current_oauth_integrations(test_user: User) -> None:
    """The manage_integration usage_hint must enumerate every OAuth integration
    registered on the current deployment, render their human-readable labels,
    and instruct the agent to trust the hint over stale tool results.

    Regression for #1261: when a new integration shipped (e.g. google_drive
    in #1251), users with prior 'manage_integration' results in their
    conversation history saw the agent claim the new integration was
    unavailable, because the agent trusted the stale tool result. The fix
    lives in the system prompt (via this usage_hint), which is rebuilt
    every turn from the live registry, so the hint always overrides stale
    tool history.
    """
    from backend.app.services.oauth import list_oauth_integrations

    ctx = ToolContext(user=test_user)
    tools = create_integration_tools(ctx)
    tool = next(t for t in tools if t.name == ToolName.MANAGE_INTEGRATION)
    assert tool.usage_hint is not None

    # Every registered OAuth integration appears as a connect target.
    for oauth_name in list_oauth_integrations():
        assert oauth_name in tool.usage_hint, (
            f"usage_hint should reference every registered OAuth integration; "
            f"missing '{oauth_name}'"
        )

    # Magic-link integrations (paste-token auth, e.g. appfolio_vendor) are
    # also routed through manage_integration and so must appear in the hint
    # for the same staleness-override reason as OAuth integrations.
    from backend.app.agent.tools.integration_tools import _MAGIC_LINK_INTEGRATIONS

    for magic_link_name in _MAGIC_LINK_INTEGRATIONS:
        assert magic_link_name in tool.usage_hint, (
            f"usage_hint should reference every magic-link integration; missing '{magic_link_name}'"
        )

    # Human-readable labels render for at least one well-known integration.
    # If google_drive is ever removed from the codebase this assertion
    # should be updated to a different known integration; the point is to
    # lock in that display names render, not just raw oauth keys.
    if "google_drive" in list_oauth_integrations():
        assert "Google Drive" in tool.usage_hint, (
            "usage_hint should render the human-readable display name "
            "('Google Drive'), not just the raw 'google_drive' key"
        )

    # The freshness instruction is the whole point of the hint: without it,
    # the model has no reason to override a stale tool result in history.
    hint_lower = tool.usage_hint.lower()
    assert "trust" in hint_lower and "earlier" in hint_lower, (
        "usage_hint should tell the agent to trust the current list over "
        "earlier (stale) manage_integration results"
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
        mock_oauth.is_connected = AsyncMock(return_value=False)
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
        mock_oauth.is_connected = AsyncMock(return_value=False)
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
        mock_oauth.is_connected = AsyncMock(return_value=False)
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
        mock_oauth.is_connected = AsyncMock(return_value=True)

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
        mock_oauth.is_connected = AsyncMock(return_value=True)
        mock_oauth.delete_token = AsyncMock(return_value=True)

        result = await _call(test_user, "disconnect", "google_calendar")
        assert not result.is_error
        assert "Disconnected" in result.content
        mock_oauth.delete_token.assert_called_once_with(test_user.id, "google_calendar")


@pytest.mark.asyncio()
async def test_disconnect_not_connected(test_user: User) -> None:
    """Disconnecting a not-connected integration should return an error."""
    with patch("backend.app.agent.tools.integration_tools.oauth_service") as mock_oauth:
        mock_oauth.is_connected = AsyncMock(return_value=False)

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


# ---------------------------------------------------------------------------
# Magic-link integrations (AppFolio Vendor Portal)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_status_lists_appfolio_with_connection_state(test_user: User) -> None:
    """Status should surface AppFolio's magic-link connection state.

    Regression for the dev.clawbolt.ai bug where the agent told the user
    "AppFolio is listed as an integration but it's not set up yet on the
    backend" because manage_integration only knew about OAuth.
    """
    with patch(
        "backend.app.agent.tools.integration_tools.appfolio_auth.is_connected",
        new=AsyncMock(return_value=False),
    ):
        result = await _call(test_user, "status")
    assert not result.is_error
    assert "appfolio_vendor" in result.content
    assert "AppFolio Vendor Portal" in result.content
    assert "not connected" in result.content


@pytest.mark.asyncio()
async def test_status_marks_appfolio_connected(test_user: User) -> None:
    """When AppFolio has a credential, status should say 'connected'."""
    with patch(
        "backend.app.agent.tools.integration_tools.appfolio_auth.is_connected",
        new=AsyncMock(return_value=True),
    ):
        result = await _call(test_user, "status")
    assert not result.is_error
    # The line for appfolio_vendor must include 'connected' (and not the
    # negated form).
    appfolio_line = next(line for line in result.content.splitlines() if "appfolio_vendor" in line)
    assert "connected" in appfolio_line
    assert "not connected" not in appfolio_line


@pytest.mark.asyncio()
async def test_connect_appfolio_returns_magic_link_instructions(test_user: User) -> None:
    """Connect with target='appfolio_vendor' should return paste-token instructions.

    The agent-facing message must mention vendor.appfolio.com (so the
    agent can guide the user to the right site) and appfolio_connect (so
    the agent knows which tool finishes the flow).
    """
    with patch(
        "backend.app.agent.tools.integration_tools.appfolio_auth.is_connected",
        new=AsyncMock(return_value=False),
    ):
        result = await _call(test_user, "connect", "appfolio_vendor")
    assert not result.is_error
    assert "vendor.appfolio.com" in result.content
    assert "appfolio_connect" in result.content
    # Should NOT claim AppFolio "does not use OAuth" or look like a rejection.
    assert "does not use OAuth" not in result.content


@pytest.mark.asyncio()
async def test_connect_appfolio_when_already_connected(test_user: User) -> None:
    """Connecting an already-connected AppFolio should report it, not re-prompt."""
    with patch(
        "backend.app.agent.tools.integration_tools.appfolio_auth.is_connected",
        new=AsyncMock(return_value=True),
    ):
        result = await _call(test_user, "connect", "appfolio_vendor")
    assert not result.is_error
    assert "already connected" in result.content


@pytest.mark.asyncio()
async def test_disconnect_appfolio_clears_credential(test_user: User) -> None:
    """Disconnecting AppFolio should call clear_credential."""
    is_connected_mock = AsyncMock(return_value=True)
    clear_mock = AsyncMock()
    with (
        patch(
            "backend.app.agent.tools.integration_tools.appfolio_auth.is_connected",
            new=is_connected_mock,
        ),
        patch(
            "backend.app.agent.tools.integration_tools.appfolio_auth.clear_credential",
            new=clear_mock,
        ),
    ):
        result = await _call(test_user, "disconnect", "appfolio_vendor")
    assert not result.is_error
    assert "Disconnected" in result.content
    clear_mock.assert_awaited_once_with(test_user.id)


@pytest.mark.asyncio()
async def test_disconnect_appfolio_when_not_connected(test_user: User) -> None:
    """Disconnecting AppFolio with no credential should return a NOT_FOUND error."""
    with patch(
        "backend.app.agent.tools.integration_tools.appfolio_auth.is_connected",
        new=AsyncMock(return_value=False),
    ):
        result = await _call(test_user, "disconnect", "appfolio_vendor")
    assert result.is_error
    assert "not currently connected" in result.content


@pytest.mark.asyncio()
async def test_appfolio_usage_hint_mentions_magic_link(test_user: User) -> None:
    """The usage_hint should tell the agent that AppFolio uses magic-link auth.

    Without this guidance the agent would assume a connect URL is coming
    back and either stall or pass the instructions through verbatim.
    """
    ctx = ToolContext(user=test_user)
    tools = create_integration_tools(ctx)
    tool = next(t for t in tools if t.name == ToolName.MANAGE_INTEGRATION)
    assert tool.usage_hint is not None
    hint = tool.usage_hint.lower()
    assert "appfolio" in hint
    assert "magic-link" in hint or "magic link" in hint


# ---------------------------------------------------------------------------
# Hidden backing factories (``appfolio_auth``)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_status_omits_hidden_appfolio_auth_factory(test_user: User) -> None:
    """``manage_integration(status)`` must not surface the backing
    ``appfolio_auth`` factory; users see only ``appfolio_vendor`` as the
    AppFolio integration.
    """
    with patch(
        "backend.app.agent.tools.integration_tools.appfolio_auth.is_connected",
        new=AsyncMock(return_value=False),
    ):
        result = await _call(test_user, "status")
    assert "appfolio_auth" not in result.content


@pytest.mark.asyncio()
@pytest.mark.parametrize("action", ["enable", "disable", "connect", "disconnect"])
async def test_hidden_factory_rejected_from_all_actions(test_user: User, action: str) -> None:
    """All ``manage_integration`` actions must reject the backing
    ``appfolio_auth`` factory as ``Unknown tool group``. The LLM never has
    a reason to address it directly; this is defense in depth in case it
    tries.
    """
    result = await _call(test_user, action, "appfolio_auth")
    assert result.is_error
    assert "Unknown tool group" in result.content
    assert "appfolio_auth" in result.content


@pytest.mark.asyncio()
async def test_disable_appfolio_vendor_cascades_to_appfolio_auth(
    test_user: User,
) -> None:
    """Disabling ``appfolio_vendor`` must also flip ``appfolio_auth`` so
    the registry's ``excluded_factories`` mechanism removes both
    factories together. Without the cascade, the auth tools would stay
    on the LLM's schema even though the user disabled the integration.
    """
    store = ToolConfigStore(test_user.id)

    result = await _call(test_user, "disable", "appfolio_vendor")
    assert not result.is_error

    disabled = await store.get_disabled_tool_names()
    assert "appfolio_vendor" in disabled
    assert "appfolio_auth" in disabled


@pytest.mark.asyncio()
async def test_enable_appfolio_vendor_cascades_to_appfolio_auth(
    test_user: User,
) -> None:
    """The complementary cascade: re-enabling ``appfolio_vendor`` must
    also re-enable ``appfolio_auth`` so the connect flow works again.
    """
    store = ToolConfigStore(test_user.id)
    await store.set_enabled("appfolio_vendor", enabled=False)
    await store.set_enabled("appfolio_auth", enabled=False)

    result = await _call(test_user, "enable", "appfolio_vendor")
    assert not result.is_error

    disabled = await store.get_disabled_tool_names()
    assert "appfolio_vendor" not in disabled
    assert "appfolio_auth" not in disabled
