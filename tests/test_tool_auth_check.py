"""Tests for tool factory auth_check and unauthenticated integration awareness."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel

from backend.app.agent.tools.base import Tool, ToolErrorKind, ToolResult
from backend.app.agent.tools.registry import (
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
