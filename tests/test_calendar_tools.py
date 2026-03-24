"""Tests for Google Calendar tools."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from backend.app.agent.approval import PermissionLevel
from backend.app.agent.tools.base import Tool, ToolErrorKind
from backend.app.agent.tools.calendar_tools import (
    _calendar_factory,
    _parse_dt,
    _resolve_tz,
    create_calendar_tools,
)
from backend.app.agent.tools.names import ToolName
from backend.app.agent.tools.registry import ToolContext
from backend.app.models import User
from tests.mocks.google_calendar import MockGoogleCalendarService


@pytest.fixture()
def cal_service() -> MockGoogleCalendarService:
    return MockGoogleCalendarService()


@pytest.fixture()
def cal_tools(cal_service: MockGoogleCalendarService) -> list[Tool]:
    return create_calendar_tools(cal_service)


def _get_tool(tools: list[Tool], name: str) -> Tool:
    for t in tools:
        if t.name == name:
            return t
    msg = f"Tool {name} not found"
    raise ValueError(msg)


# ---------------------------------------------------------------------------
# Factory tests
# ---------------------------------------------------------------------------


def test_factory_returns_empty_when_not_configured() -> None:
    """_calendar_factory should return [] when client_id/secret are empty."""
    ctx = MagicMock(spec=ToolContext)
    user = MagicMock(spec=User)
    user.id = "1"
    ctx.user = user

    with patch("backend.app.agent.tools.calendar_tools.settings") as mock_settings:
        mock_settings.google_calendar_client_id = ""
        mock_settings.google_calendar_client_secret = ""
        assert _calendar_factory(ctx) == []


def test_factory_returns_empty_when_not_connected() -> None:
    """_calendar_factory should return [] when user has no OAuth token."""
    ctx = MagicMock(spec=ToolContext)
    user = MagicMock(spec=User)
    user.id = "1"
    ctx.user = user

    with (
        patch("backend.app.agent.tools.calendar_tools.settings") as mock_settings,
        patch("backend.app.agent.tools.calendar_tools.oauth_service") as mock_oauth,
    ):
        mock_settings.google_calendar_client_id = "test-id"
        mock_settings.google_calendar_client_secret = "test-secret"
        mock_oauth.load_token.return_value = None

        tools = _calendar_factory(ctx)

    assert tools == []


def test_factory_returns_5_tools_when_configured() -> None:
    """_calendar_factory should return 5 tools when configured and connected."""
    ctx = MagicMock(spec=ToolContext)
    user = MagicMock(spec=User)
    user.id = "1"
    ctx.user = user

    mock_token = MagicMock()
    mock_token.access_token = "test-access"
    mock_token.refresh_token = "test-refresh"
    mock_token.expires_at = 9999999999.0

    with (
        patch("backend.app.agent.tools.calendar_tools.settings") as mock_settings,
        patch("backend.app.agent.tools.calendar_tools.oauth_service") as mock_oauth,
    ):
        mock_settings.google_calendar_client_id = "test-id"
        mock_settings.google_calendar_client_secret = "test-secret"
        mock_oauth.load_token.return_value = mock_token

        tools = _calendar_factory(ctx)

    assert len(tools) == 5


# ---------------------------------------------------------------------------
# Tool count and metadata
# ---------------------------------------------------------------------------


def test_calendar_tools_count(cal_tools: list[Tool]) -> None:
    """create_calendar_tools should return 5 tools."""
    assert len(cal_tools) == 5


def test_calendar_tools_have_params_model(cal_tools: list[Tool]) -> None:
    """All calendar tools must have a params_model set."""
    for tool in cal_tools:
        assert tool.params_model is not None, f"Tool {tool.name} missing params_model"


def test_calendar_tools_names(cal_tools: list[Tool]) -> None:
    """Verify all expected tool names are present."""
    names = {t.name for t in cal_tools}
    assert names == {
        ToolName.CALENDAR_LIST_EVENTS,
        ToolName.CALENDAR_CREATE_EVENT,
        ToolName.CALENDAR_UPDATE_EVENT,
        ToolName.CALENDAR_DELETE_EVENT,
        ToolName.CALENDAR_CHECK_AVAILABILITY,
    }


# ---------------------------------------------------------------------------
# Approval policies
# ---------------------------------------------------------------------------


def test_list_events_is_auto(cal_tools: list[Tool]) -> None:
    tool = _get_tool(cal_tools, ToolName.CALENDAR_LIST_EVENTS)
    assert tool.approval_policy is not None
    assert tool.approval_policy.default_level == PermissionLevel.AUTO


def test_create_event_is_ask(cal_tools: list[Tool]) -> None:
    tool = _get_tool(cal_tools, ToolName.CALENDAR_CREATE_EVENT)
    assert tool.approval_policy is not None
    assert tool.approval_policy.default_level == PermissionLevel.ASK


def test_update_event_is_ask(cal_tools: list[Tool]) -> None:
    tool = _get_tool(cal_tools, ToolName.CALENDAR_UPDATE_EVENT)
    assert tool.approval_policy is not None
    assert tool.approval_policy.default_level == PermissionLevel.ASK


def test_delete_event_is_ask(cal_tools: list[Tool]) -> None:
    tool = _get_tool(cal_tools, ToolName.CALENDAR_DELETE_EVENT)
    assert tool.approval_policy is not None
    assert tool.approval_policy.default_level == PermissionLevel.ASK


def test_check_availability_is_auto(cal_tools: list[Tool]) -> None:
    tool = _get_tool(cal_tools, ToolName.CALENDAR_CHECK_AVAILABILITY)
    assert tool.approval_policy is not None
    assert tool.approval_policy.default_level == PermissionLevel.AUTO


# ---------------------------------------------------------------------------
# Description builders
# ---------------------------------------------------------------------------


def test_create_event_description_builder(cal_tools: list[Tool]) -> None:
    tool = _get_tool(cal_tools, ToolName.CALENDAR_CREATE_EVENT)
    assert tool.approval_policy is not None
    assert tool.approval_policy.description_builder is not None
    desc = tool.approval_policy.description_builder({"title": "Job: Smith Remodel"})
    assert "Smith Remodel" in desc


def test_update_event_description_builder(cal_tools: list[Tool]) -> None:
    tool = _get_tool(cal_tools, ToolName.CALENDAR_UPDATE_EVENT)
    assert tool.approval_policy is not None
    assert tool.approval_policy.description_builder is not None
    desc = tool.approval_policy.description_builder({"event_id": "evt-001"})
    assert "evt-001" in desc


def test_delete_event_description_builder(cal_tools: list[Tool]) -> None:
    tool = _get_tool(cal_tools, ToolName.CALENDAR_DELETE_EVENT)
    assert tool.approval_policy is not None
    assert tool.approval_policy.description_builder is not None
    desc = tool.approval_policy.description_builder({"event_id": "evt-002"})
    assert "evt-002" in desc


# ---------------------------------------------------------------------------
# list_events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_list_events_happy_path(cal_tools: list[Tool]) -> None:
    """Should return sample events within the time range."""
    tool = _get_tool(cal_tools, ToolName.CALENDAR_LIST_EVENTS)
    result = await tool.function(
        start_date="2026-03-25T00:00:00",
        end_date="2026-03-27T23:59:59",
    )
    assert result.is_error is False
    assert "2 event(s)" in result.content
    assert "Smith Kitchen Remodel" in result.content
    assert "Jones Roof Repair" in result.content


@pytest.mark.asyncio()
async def test_list_events_no_results(cal_tools: list[Tool]) -> None:
    """Should handle empty result set."""
    tool = _get_tool(cal_tools, ToolName.CALENDAR_LIST_EVENTS)
    result = await tool.function(
        start_date="2026-01-01T00:00:00",
        end_date="2026-01-02T23:59:59",
    )
    assert result.is_error is False
    assert "No events found" in result.content


@pytest.mark.asyncio()
async def test_list_events_invalid_date(cal_tools: list[Tool]) -> None:
    """Should reject invalid date format."""
    tool = _get_tool(cal_tools, ToolName.CALENDAR_LIST_EVENTS)
    result = await tool.function(
        start_date="not-a-date",
        end_date="2026-03-27T23:59:59",
    )
    assert result.is_error is True
    assert result.error_kind == ToolErrorKind.VALIDATION


@pytest.mark.asyncio()
async def test_list_events_api_error(cal_service: MockGoogleCalendarService) -> None:
    """Should handle API errors gracefully."""

    async def failing(*args: object, **kwargs: object) -> list:
        raise RuntimeError("API connection failed")

    cal_service.list_events = failing  # type: ignore[assignment]
    tools = create_calendar_tools(cal_service)
    tool = _get_tool(tools, ToolName.CALENDAR_LIST_EVENTS)
    result = await tool.function(
        start_date="2026-03-25T00:00:00",
        end_date="2026-03-27T23:59:59",
    )
    assert result.is_error is True
    assert result.error_kind == ToolErrorKind.SERVICE


# ---------------------------------------------------------------------------
# create_event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_create_event_happy_path(cal_tools: list[Tool]) -> None:
    """Should create an event and return its details."""
    tool = _get_tool(cal_tools, ToolName.CALENDAR_CREATE_EVENT)
    result = await tool.function(
        title="Job: Test - Plumbing",
        start="2026-03-28T09:00:00",
        end="2026-03-28T17:00:00",
        location="789 Main St",
    )
    assert result.is_error is False
    assert "Event created" in result.content
    assert "Test - Plumbing" in result.content


@pytest.mark.asyncio()
async def test_create_event_invalid_date(cal_tools: list[Tool]) -> None:
    """Should reject invalid date format."""
    tool = _get_tool(cal_tools, ToolName.CALENDAR_CREATE_EVENT)
    result = await tool.function(
        title="Test Event",
        start="bad-date",
        end="2026-03-28T17:00:00",
    )
    assert result.is_error is True
    assert result.error_kind == ToolErrorKind.VALIDATION


@pytest.mark.asyncio()
async def test_create_event_end_before_start(cal_tools: list[Tool]) -> None:
    """Should reject end time before start time."""
    tool = _get_tool(cal_tools, ToolName.CALENDAR_CREATE_EVENT)
    result = await tool.function(
        title="Test Event",
        start="2026-03-28T17:00:00",
        end="2026-03-28T09:00:00",
    )
    assert result.is_error is True
    assert result.error_kind == ToolErrorKind.VALIDATION
    assert "after start" in result.content.lower()


@pytest.mark.asyncio()
async def test_create_event_api_error(
    cal_service: MockGoogleCalendarService,
) -> None:
    """Should handle API errors gracefully."""

    async def failing(*args: object, **kwargs: object) -> object:
        raise RuntimeError("API error")

    cal_service.create_event = failing  # type: ignore[assignment]
    tools = create_calendar_tools(cal_service)
    tool = _get_tool(tools, ToolName.CALENDAR_CREATE_EVENT)
    result = await tool.function(
        title="Test",
        start="2026-03-28T09:00:00",
        end="2026-03-28T17:00:00",
    )
    assert result.is_error is True
    assert result.error_kind == ToolErrorKind.SERVICE


# ---------------------------------------------------------------------------
# update_event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_update_event_happy_path(cal_tools: list[Tool]) -> None:
    """Should update an existing event."""
    tool = _get_tool(cal_tools, ToolName.CALENDAR_UPDATE_EVENT)
    result = await tool.function(
        event_id="evt-001",
        title="Job: Smith Kitchen Remodel (Revised)",
    )
    assert result.is_error is False
    assert "Event updated" in result.content
    assert "Revised" in result.content


@pytest.mark.asyncio()
async def test_update_event_not_found(
    cal_service: MockGoogleCalendarService,
) -> None:
    """Should handle event not found."""
    tools = create_calendar_tools(cal_service)
    tool = _get_tool(tools, ToolName.CALENDAR_UPDATE_EVENT)
    result = await tool.function(
        event_id="nonexistent",
        title="Updated",
    )
    assert result.is_error is True
    assert result.error_kind == ToolErrorKind.SERVICE


@pytest.mark.asyncio()
async def test_update_event_invalid_date(cal_tools: list[Tool]) -> None:
    """Should reject invalid date in update."""
    tool = _get_tool(cal_tools, ToolName.CALENDAR_UPDATE_EVENT)
    result = await tool.function(
        event_id="evt-001",
        start="bad-date",
    )
    assert result.is_error is True
    assert result.error_kind == ToolErrorKind.VALIDATION


# ---------------------------------------------------------------------------
# delete_event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_delete_event_happy_path(
    cal_service: MockGoogleCalendarService,
) -> None:
    """Should delete an event."""
    tools = create_calendar_tools(cal_service)
    tool = _get_tool(tools, ToolName.CALENDAR_DELETE_EVENT)
    result = await tool.function(event_id="evt-001")
    assert result.is_error is False
    assert "deleted" in result.content

    # Verify event is gone
    assert len([e for e in cal_service.events if e.id == "evt-001"]) == 0


@pytest.mark.asyncio()
async def test_delete_event_not_found(
    cal_service: MockGoogleCalendarService,
) -> None:
    """Should handle deleting non-existent event."""
    tools = create_calendar_tools(cal_service)
    tool = _get_tool(tools, ToolName.CALENDAR_DELETE_EVENT)
    result = await tool.function(event_id="nonexistent")
    assert result.is_error is True
    assert result.error_kind == ToolErrorKind.SERVICE


# ---------------------------------------------------------------------------
# check_availability
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_check_availability_busy(cal_tools: list[Tool]) -> None:
    """Should return busy slots."""
    tool = _get_tool(cal_tools, ToolName.CALENDAR_CHECK_AVAILABILITY)
    result = await tool.function(
        start_date="2026-03-25T00:00:00",
        end_date="2026-03-26T00:00:00",
    )
    assert result.is_error is False
    assert "1 busy slot(s)" in result.content


@pytest.mark.asyncio()
async def test_check_availability_free(cal_tools: list[Tool]) -> None:
    """Should report free when no busy slots."""
    tool = _get_tool(cal_tools, ToolName.CALENDAR_CHECK_AVAILABILITY)
    result = await tool.function(
        start_date="2026-01-01T00:00:00",
        end_date="2026-01-02T00:00:00",
    )
    assert result.is_error is False
    assert "free" in result.content.lower()


@pytest.mark.asyncio()
async def test_check_availability_invalid_date(cal_tools: list[Tool]) -> None:
    """Should reject invalid date format."""
    tool = _get_tool(cal_tools, ToolName.CALENDAR_CHECK_AVAILABILITY)
    result = await tool.function(
        start_date="not-valid",
        end_date="2026-03-26T00:00:00",
    )
    assert result.is_error is True
    assert result.error_kind == ToolErrorKind.VALIDATION


@pytest.mark.asyncio()
async def test_check_availability_api_error(
    cal_service: MockGoogleCalendarService,
) -> None:
    """Should handle API errors gracefully."""

    async def failing(*args: object, **kwargs: object) -> list:
        raise RuntimeError("API error")

    cal_service.check_availability = failing  # type: ignore[assignment]
    tools = create_calendar_tools(cal_service)
    tool = _get_tool(tools, ToolName.CALENDAR_CHECK_AVAILABILITY)
    result = await tool.function(
        start_date="2026-03-25T00:00:00",
        end_date="2026-03-26T00:00:00",
    )
    assert result.is_error is True
    assert result.error_kind == ToolErrorKind.SERVICE


# ---------------------------------------------------------------------------
# Timezone handling
# ---------------------------------------------------------------------------


def test_resolve_tz_valid() -> None:
    """_resolve_tz should return a ZoneInfo for valid IANA names."""
    tz = _resolve_tz("America/New_York")
    assert tz.key == "America/New_York"  # type: ignore[union-attr]


def test_resolve_tz_empty_returns_utc() -> None:
    """_resolve_tz should return UTC for empty string."""
    from datetime import UTC

    assert _resolve_tz("") is UTC


def test_resolve_tz_invalid_returns_utc() -> None:
    """_resolve_tz should return UTC for invalid timezone names."""
    from datetime import UTC

    assert _resolve_tz("Not/A/Timezone") is UTC


def test_parse_dt_uses_default_tz() -> None:
    """_parse_dt should use default_tz for naive datetime strings."""
    import zoneinfo

    eastern = zoneinfo.ZoneInfo("America/New_York")
    dt = _parse_dt("2026-03-25T09:00:00", default_tz=eastern)
    assert dt.tzinfo is eastern
    # 9 AM Eastern = 1 PM UTC (EDT is UTC-4)
    assert dt.utctimetuple().tm_hour == 13


def test_parse_dt_preserves_explicit_offset() -> None:
    """_parse_dt should not override an explicit timezone offset."""
    import zoneinfo

    eastern = zoneinfo.ZoneInfo("America/New_York")
    # Pass a string with explicit UTC offset; default_tz should be ignored
    dt = _parse_dt("2026-03-25T09:00:00+00:00", default_tz=eastern)
    assert dt.utctimetuple().tm_hour == 9


def test_parse_dt_defaults_to_utc_when_no_tz() -> None:
    """_parse_dt with no default_tz should fall back to UTC."""
    from datetime import UTC

    dt = _parse_dt("2026-03-25T09:00:00")
    assert dt.tzinfo is UTC


@pytest.mark.asyncio()
async def test_list_events_respects_user_timezone() -> None:
    """Calendar tools with a user timezone interpret naive dates locally."""
    service = MockGoogleCalendarService()
    tools = create_calendar_tools(service, user_timezone="America/New_York")
    tool = _get_tool(tools, ToolName.CALENDAR_LIST_EVENTS)

    # "March 25" in Eastern time: midnight to midnight Eastern
    result = await tool.function(
        start_date="2026-03-25T00:00:00",
        end_date="2026-03-25T23:59:59",
    )
    assert result.is_error is False
    # The 09:00 UTC event (5 AM ET) is within the Eastern day
    assert "Smith Kitchen Remodel" in result.content


@pytest.mark.asyncio()
async def test_list_events_utc_default_without_timezone() -> None:
    """Without user timezone, naive dates are interpreted as UTC."""
    service = MockGoogleCalendarService()
    tools = create_calendar_tools(service)
    tool = _get_tool(tools, ToolName.CALENDAR_LIST_EVENTS)

    # March 25 midnight-to-midnight UTC includes events at 09:00 UTC
    result = await tool.function(
        start_date="2026-03-25T00:00:00",
        end_date="2026-03-25T23:59:59",
    )
    assert result.is_error is False
    assert "Smith Kitchen Remodel" in result.content


@pytest.mark.asyncio()
async def test_factory_passes_user_timezone() -> None:
    """_calendar_factory should pass user timezone to create_calendar_tools."""
    ctx = MagicMock(spec=ToolContext)
    user = MagicMock(spec=User)
    user.id = "1"
    user.timezone = "America/New_York"
    ctx.user = user

    mock_token = MagicMock()
    mock_token.access_token = "test-access"
    mock_token.refresh_token = "test-refresh"
    mock_token.expires_at = 9999999999.0

    with (
        patch("backend.app.agent.tools.calendar_tools.settings") as mock_settings,
        patch("backend.app.agent.tools.calendar_tools.oauth_service") as mock_oauth,
        patch(
            "backend.app.agent.tools.calendar_tools.create_calendar_tools",
            wraps=create_calendar_tools,
        ) as mock_create,
    ):
        mock_settings.google_calendar_client_id = "test-id"
        mock_settings.google_calendar_client_secret = "test-secret"
        mock_oauth.load_token.return_value = mock_token

        _calendar_factory(ctx)

    mock_create.assert_called_once()
    assert mock_create.call_args.kwargs["user_timezone"] == "America/New_York"
