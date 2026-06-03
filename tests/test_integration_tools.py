"""Tests for the manage_integration chat tool."""

from unittest.mock import AsyncMock, patch

import pytest

from backend.app.agent.stores import ToolConfigStore
from backend.app.agent.tools.base import ToolResult
from backend.app.agent.tools.integration_tools import (
    _HIDDEN_CORE_FACTORIES,
    create_integration_tools,
)
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

    # Web-form integrations (paste-secret auth, e.g. appfolio_vendor,
    # servicetitan) are also routed through manage_integration and so must
    # appear in the hint for the same staleness-override reason as OAuth.
    from backend.app.agent.tools.integration_tools import _WEB_CONNECT_INTEGRATIONS

    for web_connect_name in _WEB_CONNECT_INTEGRATIONS:
        assert web_connect_name in tool.usage_hint, (
            f"usage_hint should reference every web-form integration; missing '{web_connect_name}'"
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
        # The URL is delivered via the ToolReceipt (rendered server-side),
        # never echoed in content, so the LLM can neither drop nor duplicate it.
        assert result.receipt is not None
        assert result.receipt.url == "https://accounts.google.com/o/oauth2/auth?client_id=test"
        assert "https://accounts.google.com" not in result.content
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
        assert result.receipt is not None
        assert result.receipt.url == "https://example.com/auth"
        assert "https://example.com/auth" not in result.content


@pytest.mark.asyncio()
async def test_connect_link_survives_llm_dropping_it(test_user: User) -> None:
    """Regression: the OAuth connect URL reaches the user even when the LLM's
    prose omits it.

    The original bug: a user asked to connect Gmail, the tool returned the
    URL inside ``content``, and the model paraphrased the tool result into
    "Tap the link, approve access, then message me back" -- with no link.
    The fix moves the URL into a ToolReceipt so ``append_receipts`` renders
    it server-side, independent of whatever the model wrote. This test
    reproduces the model dropping the URL and asserts the user still gets it.
    """
    from backend.app.agent.context import StoredToolInteraction, StoredToolReceipt
    from backend.app.agent.tool_summary import append_receipts

    mock_config = OAuthConfig(
        integration="gmail",
        client_id="test-id",
        client_secret="test-secret",
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/gmail.readonly"],
    )
    full_url = (
        "https://accounts.google.com/o/oauth2/v2/auth?client_id=abc.apps."
        "googleusercontent.com&redirect_uri=https%3A%2F%2Fclawbolt.ai%2Fapi"
        "%2Foauth%2Fcallback&response_type=code&scope=gmail.readonly&state=xyz"
    )
    with (
        patch(
            "backend.app.agent.tools.integration_tools.get_oauth_config",
            return_value=mock_config,
        ),
        patch("backend.app.agent.tools.integration_tools.oauth_service") as mock_oauth,
    ):
        mock_oauth.is_connected = AsyncMock(return_value=False)
        mock_oauth.get_authorization_url.return_value = full_url

        result = await _call(test_user, "connect", "gmail")

    # The tool hands the URL off via the receipt, not the LLM-facing content.
    assert not result.is_error
    assert result.receipt is not None
    assert result.receipt.url == full_url
    assert full_url not in result.content

    # Now simulate the model dropping the link in its prose, exactly like the
    # original incident. append_receipts must still surface the URL.
    stored = StoredToolInteraction(
        name=ToolName.MANAGE_INTEGRATION,
        result=result.content,
        is_error=False,
        receipt=StoredToolReceipt(
            action=result.receipt.action,
            target=result.receipt.target,
            url=result.receipt.url,
        ),
    )
    llm_prose = "Tap the link, approve access, then message me back."
    outbound = append_receipts(llm_prose, [stored])

    # The full URL (sans https://, the receipt renderer's compact form) is in
    # the message the user actually receives, and only once.
    assert "accounts.google.com/o/oauth2/v2/auth?client_id=abc" in outbound
    assert outbound.count("accounts.google.com/o/oauth2/v2/auth?client_id=abc") == 1
    assert "Tap the link" in outbound


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
async def test_connect_appfolio_directs_to_web_app(test_user: User) -> None:
    """Connect with target='appfolio_vendor' should route the user to the web app.

    The magic link is a secret, so connecting moved out of chat (issue
    #1337). The agent-facing message must point at the web app and must
    not coach the user to paste a token into the conversation.
    """
    with patch(
        "backend.app.agent.tools.integration_tools.appfolio_auth.is_connected",
        new=AsyncMock(return_value=False),
    ):
        result = await _call(test_user, "connect", "appfolio_vendor")
    assert not result.is_error
    assert "web app" in result.content.lower()
    # Must not coach a chat paste flow anymore.
    assert "magic_link_token=" not in result.content
    assert "does not use OAuth" not in result.content


@pytest.mark.asyncio()
async def test_connect_servicetitan_directs_to_web_app(test_user: User) -> None:
    """Connect with target='servicetitan' should route the user to the web app."""
    with patch(
        "backend.app.agent.tools.integration_tools.servicetitan_auth.is_connected",
        new=AsyncMock(return_value=False),
    ):
        result = await _call(test_user, "connect", "servicetitan")
    assert not result.is_error
    assert "web app" in result.content.lower()
    assert "ServiceTitan" in result.content


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
async def test_web_connect_usage_hint_points_to_web_app(test_user: User) -> None:
    """The usage_hint should tell the agent AppFolio and ServiceTitan connect
    in the web app, never over chat.

    Without this guidance the agent would assume a connect URL is coming
    back, or worse, ask the user to paste secrets into the conversation.
    """
    ctx = ToolContext(user=test_user)
    tools = create_integration_tools(ctx)
    tool = next(t for t in tools if t.name == ToolName.MANAGE_INTEGRATION)
    assert tool.usage_hint is not None
    hint = tool.usage_hint.lower()
    assert "appfolio_vendor" in hint
    assert "servicetitan" in hint
    assert "web app" in hint


# ---------------------------------------------------------------------------
# ServiceTitan web-form connect/disconnect
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_status_marks_servicetitan_connected(test_user: User) -> None:
    """When ServiceTitan has a credential, status should say 'connected'."""
    with patch(
        "backend.app.agent.tools.integration_tools.servicetitan_auth.is_connected",
        new=AsyncMock(return_value=True),
    ):
        result = await _call(test_user, "status")
    assert not result.is_error
    st_line = next(line for line in result.content.splitlines() if "servicetitan" in line)
    assert "connected" in st_line
    assert "not connected" not in st_line


@pytest.mark.asyncio()
async def test_disconnect_servicetitan_clears_credentials(test_user: User) -> None:
    """Disconnecting ServiceTitan should call clear_credentials."""
    clear_mock = AsyncMock()
    with (
        patch(
            "backend.app.agent.tools.integration_tools.servicetitan_auth.is_connected",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "backend.app.agent.tools.integration_tools.servicetitan_auth.clear_credentials",
            new=clear_mock,
        ),
    ):
        result = await _call(test_user, "disconnect", "servicetitan")
    assert not result.is_error
    assert "Disconnected" in result.content
    clear_mock.assert_awaited_once_with(test_user.id)


# ---------------------------------------------------------------------------
# Display metadata sourced from the registry (issue #1260)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_status_renders_registry_display_names_for_every_integration(
    test_user: User,
) -> None:
    """``manage_integration status`` must render the registered display
    name for every visible factory without ``integration_tools.py``
    keeping a hand-maintained name dict.

    Regression for #1260: display metadata used to live in a local
    ``_DISPLAY_NAMES`` dict that had to be edited every time a new
    integration shipped. Forgetting that step produced silent degradation
    (raw factory keys leaked into the agent's mouth). Now the metadata
    lives on each ``ToolFactory`` and integration_tools reads it through
    the registry, so simply registering a new factory is enough.
    """
    # Patch oauth_service so the status flow doesn't try to hit the DB
    # for every OAuth-backed factory; we only care about the display
    # rendering here.
    with (
        patch(
            "backend.app.agent.tools.integration_tools.oauth_service",
        ) as mock_oauth,
        patch(
            "backend.app.agent.tools.integration_tools.appfolio_auth.is_connected",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "backend.app.agent.tools.integration_tools.servicetitan_auth.is_connected",
            new=AsyncMock(return_value=False),
        ),
    ):
        mock_oauth.is_connected = AsyncMock(return_value=False)
        result = await _call(test_user, "status")

    assert not result.is_error

    # Every visible factory's registered display name must show up in the
    # output. We assert this by walking the registry, not by listing
    # integration names in the test, so adding a new integration with a
    # display_name "just works" without updating this assertion.
    for name in default_registry.factory_names:
        if name in _HIDDEN_CORE_FACTORIES:
            continue
        factory = default_registry.get_factory(name)
        assert factory is not None
        # Skip factories that didn't declare a display_name; those still
        # render with the raw factory name as fallback (preserving prior
        # behavior for utility tools like ``calculator`` / ``media``).
        if not factory.display_name:
            continue
        assert factory.display_name in result.content, (
            f"factory {name!r} registered display_name "
            f"{factory.display_name!r} but status output did not render it; "
            f"output was:\n{result.content}"
        )


def test_user_facing_integrations_declare_display_names() -> None:
    """Every user-facing OAuth or magic-link integration must declare a
    ``display_name`` at registration so manage_integration never falls
    back to the raw factory key in agent output.

    This pins the contract from #1260: integration packages own their
    display label, not ``integration_tools.py``. A new integration that
    forgets to set ``display_name`` should fail this test rather than
    silently leaking ``companycam`` / ``google_drive`` style raw keys
    into chat.
    """
    from backend.app.agent.tools.integration_tools import _WEB_CONNECT_INTEGRATIONS
    from backend.app.services.oauth import list_oauth_integrations

    # Every OAuth integration must be reachable through some factory's
    # registered ``oauth_name`` (or share its name with a factory) and
    # that factory must declare a display_name.
    for oauth_name in list_oauth_integrations():
        factory_name = default_registry.find_factory_by_oauth_name(oauth_name)
        assert factory_name is not None, (
            f"OAuth integration {oauth_name!r} is registered in "
            f"_OAUTH_INTEGRATIONS but no factory's oauth_name maps to it"
        )
        factory = default_registry.get_factory(factory_name)
        assert factory is not None
        assert factory.display_name, (
            f"Factory {factory_name!r} (OAuth {oauth_name!r}) must declare "
            f"a display_name in its registry.register() call"
        )

    # Web-form integrations share the factory name with the target.
    for target in _WEB_CONNECT_INTEGRATIONS:
        factory = default_registry.get_factory(target)
        assert factory is not None, (
            f"Web-form integration {target!r} must be registered as a factory"
        )
        assert factory.display_name, f"Web-form factory {target!r} must declare a display_name"


@pytest.mark.asyncio()
async def test_connect_renders_registry_display_name(test_user: User) -> None:
    """Connect output must use the factory's registered display_name.

    Cross-check that the registry-sourced display label survives the
    full ``_handle_connect`` path, not just status. Uses google_drive
    because its OAuth name (``google_drive``) differs from its factory
    name (``file``), exercising the oauth_name -> factory display_name
    redirection.
    """
    mock_config = OAuthConfig(
        integration="google_drive",
        client_id="test-id",
        client_secret="test-secret",
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/drive.file"],
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

        result = await _call(test_user, "connect", "google_drive")

    assert not result.is_error
    # The display name is registered on the ``file`` factory; without
    # the registry plumbing this would fall back to ``google_drive``.
    assert "Google Drive" in result.content


# ---------------------------------------------------------------------------
# get_user_connected_integrations (live status feed for the system prompt)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_user_connected_integrations_skips_unconfigured_oauth() -> None:
    """OAuth integrations the operator has not wired must be omitted entirely.

    The system prompt only shows integrations the user could plausibly
    connect; surfacing "not_connected: foo" for something the operator
    cannot actually offer would confuse the model into pitching a flow
    that never works.
    """
    from backend.app.agent.tools.integration_tools import (
        get_user_connected_integrations,
    )

    configured = OAuthConfig(
        integration="google_drive",
        client_id="id",
        client_secret="secret",
        authorize_url="x",
        token_url="x",
        scopes=["scope"],
    )

    def fake_get_oauth_config(name: str) -> OAuthConfig | None:
        return configured if name == "google_drive" else None

    with (
        patch(
            "backend.app.agent.tools.integration_tools.list_oauth_integrations",
            return_value=("google_drive", "gmail"),
        ),
        patch(
            "backend.app.agent.tools.integration_tools.get_oauth_config",
            side_effect=fake_get_oauth_config,
        ),
        patch("backend.app.agent.tools.integration_tools.oauth_service") as mock_oauth_service,
        patch(
            "backend.app.agent.tools.integration_tools._is_web_connect_connected",
            new_callable=AsyncMock,
            return_value=False,
        ),
    ):
        mock_oauth_service.is_connected = AsyncMock(return_value=True)
        result = await get_user_connected_integrations("user-1")

    # gmail is unconfigured -> not in the dict at all.
    assert "gmail" not in result
    assert result["google_drive"] is True
    # Magic-link integrations still appear: they have no separate
    # "configured" check; the magic-link plumbing is always available
    # when the integration is registered.
    assert "appfolio_vendor" in result


@pytest.mark.asyncio
async def test_get_user_connected_integrations_reflects_per_user_state() -> None:
    """Connection state is read live per user; no caching."""
    from backend.app.agent.tools.integration_tools import (
        get_user_connected_integrations,
    )

    configured = OAuthConfig(
        integration="google_drive",
        client_id="id",
        client_secret="secret",
        authorize_url="x",
        token_url="x",
        scopes=["scope"],
    )

    async def fake_is_connected(user_id: str, integration: str) -> bool:
        return user_id == "alice" and integration == "google_drive"

    with (
        patch(
            "backend.app.agent.tools.integration_tools.list_oauth_integrations",
            return_value=("google_drive",),
        ),
        patch(
            "backend.app.agent.tools.integration_tools.get_oauth_config",
            return_value=configured,
        ),
        patch("backend.app.agent.tools.integration_tools.oauth_service") as mock_oauth_service,
        patch(
            "backend.app.agent.tools.integration_tools._is_web_connect_connected",
            new_callable=AsyncMock,
            return_value=False,
        ),
    ):
        mock_oauth_service.is_connected = AsyncMock(side_effect=fake_is_connected)
        alice = await get_user_connected_integrations("alice")
        bob = await get_user_connected_integrations("bob")

    assert alice["google_drive"] is True
    assert bob["google_drive"] is False
