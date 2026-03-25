"""Tests for tool factory auth_check and unauthenticated integration awareness."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel

from backend.app.agent.tools.base import Tool, ToolErrorKind, ToolResult
from backend.app.agent.tools.registry import (
    SubToolInfo,
    ToolContext,
    ToolRegistry,
    create_list_capabilities_tool,
)
from backend.app.models import User


class _EmptyParams(BaseModel):
    """Minimal stand-in so the params_model check passes."""


def _make_tool(name: str) -> Tool:
    """Create a trivial tool for testing."""

    async def noop() -> ToolResult:
        return ToolResult(content="ok")

    return Tool(name=name, description=f"test {name}", function=noop, params_model=_EmptyParams)


def _build_auth_test_registry() -> ToolRegistry:
    """Build a registry with auth_check-enabled specialists."""
    registry = ToolRegistry()
    registry.register("workspace", lambda ctx: [_make_tool("read_file")])
    # Specialist that passes auth (authenticated)
    registry.register(
        "heartbeat",
        lambda ctx: [_make_tool("get_heartbeat")],
        core=False,
        summary="Manage heartbeats",
        auth_check=lambda ctx: None,  # always authenticated
    )
    # Specialist that fails auth (not authenticated)
    registry.register(
        "quickbooks",
        lambda ctx: [],
        core=False,
        summary="QuickBooks accounting tools",
        auth_check=lambda ctx: "QuickBooks is not connected. Authenticate via web dashboard.",
    )
    # Specialist without auth_check (legacy, always available)
    registry.register(
        "file",
        lambda ctx: [_make_tool("upload_to_storage")],
        requires_storage=True,
        core=False,
        summary="Upload and organize files",
    )
    return registry


class TestGetAvailableSpecialistSummaries:
    """get_available_specialist_summaries excludes unauthenticated factories."""

    def test_excludes_unauthenticated_specialist(self) -> None:
        registry = _build_auth_test_registry()
        ctx = ToolContext(user=User(id="1"), storage=MagicMock())
        summaries = registry.get_available_specialist_summaries(ctx)
        assert "heartbeat" in summaries
        assert "file" in summaries
        assert "quickbooks" not in summaries

    def test_includes_factory_without_auth_check(self) -> None:
        registry = _build_auth_test_registry()
        ctx = ToolContext(user=User(id="1"), storage=MagicMock())
        summaries = registry.get_available_specialist_summaries(ctx)
        assert "file" in summaries

    def test_includes_factory_with_passing_auth_check(self) -> None:
        registry = _build_auth_test_registry()
        ctx = ToolContext(user=User(id="1"))
        summaries = registry.get_available_specialist_summaries(ctx)
        assert "heartbeat" in summaries


class TestGetUnauthenticatedSpecialists:
    """get_unauthenticated_specialists returns only auth-failing factories."""

    def test_returns_unauthenticated_factory(self) -> None:
        registry = _build_auth_test_registry()
        ctx = ToolContext(user=User(id="1"), storage=MagicMock())
        unauth = registry.get_unauthenticated_specialists(ctx)
        assert "quickbooks" in unauth
        assert "not connected" in unauth["quickbooks"].lower()

    def test_excludes_authenticated_factory(self) -> None:
        registry = _build_auth_test_registry()
        ctx = ToolContext(user=User(id="1"))
        unauth = registry.get_unauthenticated_specialists(ctx)
        assert "heartbeat" not in unauth

    def test_excludes_factory_without_auth_check(self) -> None:
        registry = _build_auth_test_registry()
        ctx = ToolContext(user=User(id="1"), storage=MagicMock())
        unauth = registry.get_unauthenticated_specialists(ctx)
        assert "file" not in unauth

    def test_excludes_core_factories(self) -> None:
        registry = _build_auth_test_registry()
        ctx = ToolContext(user=User(id="1"))
        unauth = registry.get_unauthenticated_specialists(ctx)
        assert "workspace" not in unauth

    def test_respects_excluded_factories(self) -> None:
        registry = _build_auth_test_registry()
        ctx = ToolContext(user=User(id="1"))
        unauth = registry.get_unauthenticated_specialists(ctx, excluded_factories={"quickbooks"})
        assert "quickbooks" not in unauth

    def test_empty_when_all_authenticated(self) -> None:
        registry = ToolRegistry()
        registry.register(
            "heartbeat",
            lambda ctx: [_make_tool("get_heartbeat")],
            core=False,
            summary="Manage heartbeats",
            auth_check=lambda ctx: None,
        )
        ctx = ToolContext(user=User(id="1"))
        unauth = registry.get_unauthenticated_specialists(ctx)
        assert unauth == {}


class TestListCapabilitiesWithUnauthenticated:
    """list_capabilities shows unauthenticated integrations and blocks activation."""

    @pytest.mark.asyncio
    async def test_listing_shows_unauthenticated_section(self) -> None:
        summaries = {"heartbeat": "Manage heartbeats"}
        unauth = {"quickbooks": "QuickBooks is not connected."}
        tool = create_list_capabilities_tool(summaries, unauthenticated=unauth)
        result = await tool.function(category=None)
        assert "heartbeat" in result.content
        assert "quickbooks" in result.content
        assert "not connected" in result.content.lower()
        assert not result.is_error

    @pytest.mark.asyncio
    async def test_activating_unauthenticated_returns_auth_error(self) -> None:
        summaries = {"heartbeat": "Manage heartbeats"}
        unauth = {"quickbooks": "QuickBooks is not connected. Authenticate via web dashboard."}
        tool = create_list_capabilities_tool(summaries, unauthenticated=unauth)
        result = await tool.function(category="quickbooks")
        assert result.is_error
        assert result.error_kind == ToolErrorKind.AUTH
        assert "not connected" in result.content.lower()

    @pytest.mark.asyncio
    async def test_activating_authenticated_category_still_works(self) -> None:
        summaries = {"heartbeat": "Manage heartbeats"}
        unauth = {"quickbooks": "QuickBooks is not connected."}
        tool = create_list_capabilities_tool(summaries, unauthenticated=unauth)
        result = await tool.function(category="heartbeat")
        assert not result.is_error
        assert "activated" in result.content.lower()

    @pytest.mark.asyncio
    async def test_usage_hint_mentions_unauthenticated(self) -> None:
        summaries = {"heartbeat": "Manage heartbeats"}
        unauth = {"quickbooks": "QuickBooks is not connected."}
        tool = create_list_capabilities_tool(summaries, unauthenticated=unauth)
        assert "quickbooks" in tool.usage_hint.lower()
        assert "not connected" in tool.usage_hint.lower()

    @pytest.mark.asyncio
    async def test_no_unauthenticated_no_extra_section(self) -> None:
        summaries = {"heartbeat": "Manage heartbeats"}
        tool = create_list_capabilities_tool(summaries)
        result = await tool.function(category=None)
        assert "not connected" not in result.content.lower()

    @pytest.mark.asyncio
    async def test_only_unauthenticated_still_shows_info(self) -> None:
        tool = create_list_capabilities_tool({}, unauthenticated={"quickbooks": "Not connected."})
        result = await tool.function(category=None)
        assert "quickbooks" in result.content
        assert "not connected" in result.content.lower()
        assert not result.is_error


class TestQuickBooksAuthCheck:
    """QuickBooks auth_check function works correctly."""

    def test_returns_none_when_not_configured(self) -> None:
        from unittest.mock import patch

        from backend.app.agent.tools.quickbooks_tools import _quickbooks_auth_check

        with patch("backend.app.agent.tools.quickbooks_tools.settings") as mock_settings:
            mock_settings.quickbooks_client_id = ""
            mock_settings.quickbooks_client_secret = ""
            ctx = ToolContext(user=User(id="test-user"))
            assert _quickbooks_auth_check(ctx) is None

    def test_returns_none_when_authenticated(self) -> None:
        from unittest.mock import patch

        from backend.app.agent.tools.quickbooks_tools import _quickbooks_auth_check

        mock_token = MagicMock()
        mock_token.access_token = "valid-token"
        mock_token.realm_id = "realm-123"
        with (
            patch("backend.app.agent.tools.quickbooks_tools.settings") as mock_settings,
            patch("backend.app.agent.tools.quickbooks_tools.oauth_service") as mock_oauth,
        ):
            mock_settings.quickbooks_client_id = "client-id"
            mock_settings.quickbooks_client_secret = "client-secret"
            mock_oauth.load_token.return_value = mock_token
            ctx = ToolContext(user=User(id="test-user"))
            assert _quickbooks_auth_check(ctx) is None

    def test_returns_reason_when_no_token(self) -> None:
        from unittest.mock import patch

        from backend.app.agent.tools.quickbooks_tools import _quickbooks_auth_check

        with (
            patch("backend.app.agent.tools.quickbooks_tools.settings") as mock_settings,
            patch("backend.app.agent.tools.quickbooks_tools.oauth_service") as mock_oauth,
        ):
            mock_settings.quickbooks_client_id = "client-id"
            mock_settings.quickbooks_client_secret = "client-secret"
            mock_oauth.load_token.return_value = None
            ctx = ToolContext(user=User(id="test-user"))
            reason = _quickbooks_auth_check(ctx)
            assert reason is not None
            assert "not connected" in reason.lower()


class TestCalendarAuthCheck:
    """Google Calendar auth_check function works correctly."""

    def test_returns_none_when_not_configured(self) -> None:
        from unittest.mock import patch

        from backend.app.agent.tools.calendar_tools import _calendar_auth_check

        with patch("backend.app.agent.tools.calendar_tools.settings") as mock_settings:
            mock_settings.google_calendar_client_id = ""
            mock_settings.google_calendar_client_secret = ""
            ctx = ToolContext(user=User(id="test-user"))
            assert _calendar_auth_check(ctx) is None

    def test_returns_none_when_authenticated(self) -> None:
        from unittest.mock import patch

        from backend.app.agent.tools.calendar_tools import _calendar_auth_check

        mock_token = MagicMock()
        mock_token.access_token = "valid-token"
        with (
            patch("backend.app.agent.tools.calendar_tools.settings") as mock_settings,
            patch("backend.app.agent.tools.calendar_tools.oauth_service") as mock_oauth,
        ):
            mock_settings.google_calendar_client_id = "client-id"
            mock_settings.google_calendar_client_secret = "client-secret"
            mock_oauth.load_token.return_value = mock_token
            ctx = ToolContext(user=User(id="test-user"))
            assert _calendar_auth_check(ctx) is None

    def test_returns_reason_when_no_token(self) -> None:
        from unittest.mock import patch

        from backend.app.agent.tools.calendar_tools import _calendar_auth_check

        with (
            patch("backend.app.agent.tools.calendar_tools.settings") as mock_settings,
            patch("backend.app.agent.tools.calendar_tools.oauth_service") as mock_oauth,
        ):
            mock_settings.google_calendar_client_id = "client-id"
            mock_settings.google_calendar_client_secret = "client-secret"
            mock_oauth.load_token.return_value = None
            ctx = ToolContext(user=User(id="test-user"))
            reason = _calendar_auth_check(ctx)
            assert reason is not None
            assert "not connected" in reason.lower()


# ---------------------------------------------------------------------------
# Registry: get_disabled_specialist_sub_tools / get_factory_for_sub_tool
# ---------------------------------------------------------------------------


def _build_sub_tool_registry() -> ToolRegistry:
    """Build a registry with specialist sub-tools for testing."""
    registry = ToolRegistry()
    registry.register("workspace", lambda ctx: [_make_tool("read_file")])
    registry.register(
        "quickbooks",
        lambda ctx: [],
        core=False,
        summary="QuickBooks accounting tools",
        sub_tools=[
            SubToolInfo("qb_query", "Query QB entities"),
            SubToolInfo("qb_create", "Create QB entities"),
            SubToolInfo("qb_update", "Update QB entities"),
        ],
    )
    registry.register(
        "calendar",
        lambda ctx: [],
        core=False,
        summary="Google Calendar tools",
        sub_tools=[
            SubToolInfo("calendar_list_events", "List events"),
            SubToolInfo("calendar_create_event", "Create events"),
        ],
    )
    return registry


class TestGetDisabledSpecialistSubTools:
    """get_disabled_specialist_sub_tools maps specialists to disabled sub-tools."""

    def test_returns_correct_disabled(self) -> None:
        registry = _build_sub_tool_registry()
        result = registry.get_disabled_specialist_sub_tools({"qb_create", "qb_update"})
        assert "quickbooks" in result
        assert len(result["quickbooks"]) == 2
        names = {st.name for st in result["quickbooks"]}
        assert names == {"qb_create", "qb_update"}

    def test_empty_set_returns_empty_dict(self) -> None:
        registry = _build_sub_tool_registry()
        assert registry.get_disabled_specialist_sub_tools(set()) == {}

    def test_ignores_core_factories(self) -> None:
        registry = ToolRegistry()
        registry.register(
            "workspace",
            lambda ctx: [_make_tool("read_file")],
            core=True,
            sub_tools=[SubToolInfo("read_file", "Read a file")],
        )
        result = registry.get_disabled_specialist_sub_tools({"read_file"})
        assert "workspace" not in result

    def test_multiple_specialists(self) -> None:
        registry = _build_sub_tool_registry()
        result = registry.get_disabled_specialist_sub_tools({"qb_create", "calendar_create_event"})
        assert "quickbooks" in result
        assert "calendar" in result
        assert len(result["quickbooks"]) == 1
        assert result["quickbooks"][0].name == "qb_create"
        assert result["calendar"][0].name == "calendar_create_event"

    def test_no_match_returns_empty(self) -> None:
        registry = _build_sub_tool_registry()
        result = registry.get_disabled_specialist_sub_tools({"nonexistent_tool"})
        assert result == {}


# ---------------------------------------------------------------------------
# list_capabilities with disabled sub-tools
# ---------------------------------------------------------------------------


class TestListCapabilitiesWithDisabledSubTools:
    """list_capabilities shows disabled sub-tool info when provided."""

    @pytest.mark.asyncio
    async def test_listing_shows_disabled_info(self) -> None:
        summaries = {"quickbooks": "QB tools"}
        disabled = {
            "quickbooks": [SubToolInfo("qb_create", "Create"), SubToolInfo("qb_update", "Update")]
        }
        tool = create_list_capabilities_tool(summaries, disabled_sub_tools=disabled)
        result = await tool.function(category=None)
        assert "qb_create" in result.content
        assert "qb_update" in result.content
        assert "disabled" in result.content.lower()

    @pytest.mark.asyncio
    async def test_activation_notes_disabled_tools(self) -> None:
        summaries = {"quickbooks": "QB tools"}
        disabled = {"quickbooks": [SubToolInfo("qb_create", "Create")]}
        tool = create_list_capabilities_tool(summaries, disabled_sub_tools=disabled)
        result = await tool.function(category="quickbooks")
        assert not result.is_error
        assert "activated" in result.content.lower()
        assert "qb_create" in result.content
        assert "disabled" in result.content.lower()

    @pytest.mark.asyncio
    async def test_no_disabled_no_change(self) -> None:
        summaries = {"quickbooks": "QB tools"}
        tool = create_list_capabilities_tool(summaries)
        result = await tool.function(category=None)
        assert "disabled" not in result.content.lower()

    @pytest.mark.asyncio
    async def test_activation_without_disabled_no_note(self) -> None:
        summaries = {"quickbooks": "QB tools"}
        tool = create_list_capabilities_tool(summaries)
        result = await tool.function(category="quickbooks")
        assert "disabled" not in result.content.lower()

    def test_usage_hint_updated_with_disabled(self) -> None:
        summaries = {"quickbooks": "QB tools"}
        disabled = {"quickbooks": [SubToolInfo("qb_create", "Create")]}
        tool = create_list_capabilities_tool(summaries, disabled_sub_tools=disabled)
        assert "disabled" in tool.usage_hint.lower()

    def test_usage_hint_no_disabled(self) -> None:
        summaries = {"quickbooks": "QB tools"}
        tool = create_list_capabilities_tool(summaries)
        assert "disabled" not in tool.usage_hint.lower()
