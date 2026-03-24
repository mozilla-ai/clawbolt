"""Google Calendar tools for the agent."""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx
from pydantic import BaseModel, Field

from backend.app.agent.approval import ApprovalPolicy, PermissionLevel
from backend.app.agent.tools.base import Tool, ToolErrorKind, ToolResult
from backend.app.agent.tools.names import ToolName
from backend.app.config import settings
from backend.app.services.calendar_provider import (
    CalendarEventCreate,
    CalendarEventUpdate,
)
from backend.app.services.google_calendar import GoogleCalendarService
from backend.app.services.oauth import (
    OAuthTokenData,
    oauth_service,
)

if TYPE_CHECKING:
    from backend.app.agent.tools.registry import ToolContext

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Param models
# ---------------------------------------------------------------------------


class CalendarListEventsParams(BaseModel):
    """Parameters for the calendar_list_events tool."""

    start_date: str = Field(
        description=("Start of the time range in ISO 8601 format (e.g. 2026-03-23T00:00:00).")
    )
    end_date: str = Field(
        description=("End of the time range in ISO 8601 format (e.g. 2026-03-30T23:59:59).")
    )
    calendar_id: str = Field(
        default="primary",
        description="Calendar ID to query. Defaults to 'primary'.",
    )


class CalendarCreateEventParams(BaseModel):
    """Parameters for the calendar_create_event tool."""

    title: str = Field(description="Event title (e.g. 'Job: Smith Kitchen Remodel').")
    start: str = Field(description="Event start in ISO 8601 format (e.g. 2026-03-25T09:00:00).")
    end: str = Field(description="Event end in ISO 8601 format (e.g. 2026-03-25T17:00:00).")
    description: str = Field(default="", description="Event description or notes.")
    location: str = Field(default="", description="Event location address.")
    calendar_id: str = Field(
        default="primary",
        description="Calendar ID to create the event on. Defaults to 'primary'.",
    )


class CalendarUpdateEventParams(BaseModel):
    """Parameters for the calendar_update_event tool."""

    event_id: str = Field(description="Google Calendar event ID to update.")
    title: str | None = Field(default=None, description="New event title.")
    start: str | None = Field(default=None, description="New start in ISO 8601 format.")
    end: str | None = Field(default=None, description="New end in ISO 8601 format.")
    description: str | None = Field(default=None, description="New description.")
    location: str | None = Field(default=None, description="New location.")
    calendar_id: str = Field(
        default="primary",
        description="Calendar ID. Defaults to 'primary'.",
    )


class CalendarDeleteEventParams(BaseModel):
    """Parameters for the calendar_delete_event tool."""

    event_id: str = Field(description="Google Calendar event ID to delete.")
    calendar_id: str = Field(
        default="primary",
        description="Calendar ID. Defaults to 'primary'.",
    )


class CalendarCheckAvailabilityParams(BaseModel):
    """Parameters for the calendar_check_availability tool."""

    start_date: str = Field(description="Start of the range in ISO 8601 format.")
    end_date: str = Field(description="End of the range in ISO 8601 format.")
    calendar_id: str = Field(
        default="primary",
        description="Calendar ID to check. Defaults to 'primary'.",
    )


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _format_event(event: Any) -> str:
    """Format a single calendar event for LLM readability."""
    parts = [event.title]
    if event.all_day:
        parts.append(f"(all day, {event.start.strftime('%Y-%m-%d')})")
    else:
        parts.append(f"{event.start.strftime('%Y-%m-%d %H:%M')} - {event.end.strftime('%H:%M')}")
    if event.location:
        parts.append(f"@ {event.location}")
    if event.description:
        desc = event.description[:100]
        if len(event.description) > 100:
            desc += "..."
        parts.append(f"| {desc}")
    parts.append(f"[id: {event.id}]")
    return " | ".join(parts)


def _parse_dt(value: str) -> datetime:
    """Parse an ISO 8601 datetime string. Assumes UTC if no timezone."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


# ---------------------------------------------------------------------------
# Token refresh callback
# ---------------------------------------------------------------------------


def _make_token_refresh_callback(user_id: str) -> Any:
    """Return a callback that persists refreshed tokens to disk."""

    def _persist_refreshed_tokens(access_token: str, refresh_token: str) -> None:
        try:
            token = oauth_service.load_token(user_id, "google_calendar")
            if token is None:
                token = OAuthTokenData(
                    access_token=access_token,
                    refresh_token=refresh_token,
                )
            else:
                token.access_token = access_token
                token.refresh_token = refresh_token
                token.expires_at = time.time() + 3600
            oauth_service.save_token(user_id, "google_calendar", token)
        except Exception:
            logger.exception(
                "Failed to persist refreshed Google Calendar tokens for user %s",
                user_id,
            )

    return _persist_refreshed_tokens


# ---------------------------------------------------------------------------
# Tool creation
# ---------------------------------------------------------------------------


def create_calendar_tools(
    service: GoogleCalendarService | Any,
) -> list[Tool]:
    """Create calendar tools bound to a calendar service instance."""

    async def calendar_list_events(
        start_date: str, end_date: str, calendar_id: str = "primary"
    ) -> ToolResult:
        """List calendar events in a time range."""
        try:
            time_min = _parse_dt(start_date)
            time_max = _parse_dt(end_date)
        except ValueError as exc:
            return ToolResult(
                content=f"Invalid date format: {exc}. Use ISO 8601 (e.g. 2026-03-23T00:00:00).",
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
            )

        try:
            events = await service.list_events(calendar_id, time_min, time_max)
        except httpx.TimeoutException:
            return ToolResult(
                content="Calendar service unavailable (timeout). Try again shortly.",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )
        except httpx.HTTPStatusError as exc:
            return _handle_http_error(exc, "list events")
        except Exception as exc:
            logger.exception("Calendar list_events failed")
            return ToolResult(
                content=f"Calendar error: {exc}",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )

        if not events:
            return ToolResult(content=f"No events found between {start_date} and {end_date}.")

        lines = [f"Found {len(events)} event(s):"]
        for event in events:
            lines.append(f"- {_format_event(event)}")
        return ToolResult(content="\n".join(lines))

    async def calendar_create_event(
        title: str,
        start: str,
        end: str,
        description: str = "",
        location: str = "",
        calendar_id: str = "primary",
    ) -> ToolResult:
        """Create a new calendar event."""
        try:
            start_dt = _parse_dt(start)
            end_dt = _parse_dt(end)
        except ValueError as exc:
            return ToolResult(
                content=f"Invalid date format: {exc}. Use ISO 8601.",
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
            )

        if end_dt <= start_dt:
            return ToolResult(
                content="End time must be after start time.",
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
            )

        create_data = CalendarEventCreate(
            title=title,
            start=start_dt,
            end=end_dt,
            description=description,
            location=location,
        )

        try:
            event = await service.create_event(calendar_id, create_data)
        except httpx.TimeoutException:
            return ToolResult(
                content="Calendar service unavailable (timeout). Try again shortly.",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )
        except httpx.HTTPStatusError as exc:
            return _handle_http_error(exc, "create event")
        except Exception as exc:
            logger.exception("Calendar create_event failed")
            return ToolResult(
                content=f"Failed to create event: {exc}",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )

        return ToolResult(
            content=(
                f"Event created: {event.title} | "
                f"{event.start.strftime('%Y-%m-%d %H:%M')} - "
                f"{event.end.strftime('%H:%M')} | "
                f"id: {event.id}"
            )
        )

    async def calendar_update_event(
        event_id: str,
        title: str | None = None,
        start: str | None = None,
        end: str | None = None,
        description: str | None = None,
        location: str | None = None,
        calendar_id: str = "primary",
    ) -> ToolResult:
        """Update an existing calendar event."""
        start_dt = None
        end_dt = None
        if start is not None:
            try:
                start_dt = _parse_dt(start)
            except ValueError as exc:
                return ToolResult(
                    content=f"Invalid start date: {exc}. Use ISO 8601.",
                    is_error=True,
                    error_kind=ToolErrorKind.VALIDATION,
                )
        if end is not None:
            try:
                end_dt = _parse_dt(end)
            except ValueError as exc:
                return ToolResult(
                    content=f"Invalid end date: {exc}. Use ISO 8601.",
                    is_error=True,
                    error_kind=ToolErrorKind.VALIDATION,
                )

        updates = CalendarEventUpdate(
            title=title,
            start=start_dt,
            end=end_dt,
            description=description,
            location=location,
        )

        try:
            event = await service.update_event(calendar_id, event_id, updates)
        except httpx.TimeoutException:
            return ToolResult(
                content="Calendar service unavailable (timeout). Try again shortly.",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )
        except httpx.HTTPStatusError as exc:
            return _handle_http_error(exc, f"update event {event_id}")
        except Exception as exc:
            logger.exception("Calendar update_event failed")
            return ToolResult(
                content=f"Failed to update event: {exc}",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )

        return ToolResult(
            content=(
                f"Event updated: {event.title} | "
                f"{event.start.strftime('%Y-%m-%d %H:%M')} - "
                f"{event.end.strftime('%H:%M')} | "
                f"id: {event.id}"
            )
        )

    async def calendar_delete_event(
        event_id: str,
        calendar_id: str = "primary",
    ) -> ToolResult:
        """Delete a calendar event."""
        try:
            await service.delete_event(calendar_id, event_id)
        except httpx.TimeoutException:
            return ToolResult(
                content="Calendar service unavailable (timeout). Try again shortly.",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )
        except httpx.HTTPStatusError as exc:
            return _handle_http_error(exc, f"delete event {event_id}")
        except Exception as exc:
            logger.exception("Calendar delete_event failed")
            return ToolResult(
                content=f"Failed to delete event: {exc}",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )

        return ToolResult(content=f"Event {event_id} deleted.")

    async def calendar_check_availability(
        start_date: str, end_date: str, calendar_id: str = "primary"
    ) -> ToolResult:
        """Check calendar availability (free/busy) in a time range."""
        try:
            time_min = _parse_dt(start_date)
            time_max = _parse_dt(end_date)
        except ValueError as exc:
            return ToolResult(
                content=f"Invalid date format: {exc}. Use ISO 8601.",
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
            )

        try:
            busy_slots = await service.check_availability(calendar_id, time_min, time_max)
        except httpx.TimeoutException:
            return ToolResult(
                content="Calendar service unavailable (timeout). Try again shortly.",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )
        except httpx.HTTPStatusError as exc:
            return _handle_http_error(exc, "check availability")
        except Exception as exc:
            logger.exception("Calendar check_availability failed")
            return ToolResult(
                content=f"Calendar error: {exc}",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )

        if not busy_slots:
            return ToolResult(content=f"Calendar is free between {start_date} and {end_date}.")

        lines = [f"Found {len(busy_slots)} busy slot(s):"]
        for slot in busy_slots:
            lines.append(
                f"- {slot.start.strftime('%Y-%m-%d %H:%M')} - {slot.end.strftime('%H:%M')}"
            )
        return ToolResult(content="\n".join(lines))

    return [
        Tool(
            name=ToolName.CALENDAR_LIST_EVENTS,
            description=(
                "List events on Google Calendar within a date range. "
                "Returns event titles, times, locations, and IDs."
            ),
            function=calendar_list_events,
            params_model=CalendarListEventsParams,
            usage_hint=(
                "List upcoming calendar events. Use ISO 8601 dates. "
                "Always check the calendar before scheduling new events."
            ),
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.AUTO,
            ),
        ),
        Tool(
            name=ToolName.CALENDAR_CREATE_EVENT,
            description=(
                "Create a new event on Google Calendar. "
                "Use 'Job: {client} - {description}' format for job events. "
                "Include the job location."
            ),
            function=calendar_create_event,
            params_model=CalendarCreateEventParams,
            usage_hint=(
                "Create a calendar event. Check availability first. "
                "Use 'Job: Client - Description' format for job titles."
            ),
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ASK,
                resource_extractor=lambda args: "create event",
                description_builder=lambda args: (
                    f"Create calendar event: {args.get('title', 'event')}"
                ),
            ),
        ),
        Tool(
            name=ToolName.CALENDAR_UPDATE_EVENT,
            description=(
                "Update an existing Google Calendar event. "
                "Pass the event_id from a prior calendar_list_events call "
                "and only the fields to change."
            ),
            function=calendar_update_event,
            params_model=CalendarUpdateEventParams,
            usage_hint=(
                "Update an existing event. Get the event_id from calendar_list_events first."
            ),
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ASK,
                resource_extractor=lambda args: f"update {args.get('event_id', '')}",
                description_builder=lambda args: (
                    f"Update calendar event {args.get('event_id', '')}"
                ),
            ),
        ),
        Tool(
            name=ToolName.CALENDAR_DELETE_EVENT,
            description=(
                "Delete an event from Google Calendar. "
                "Pass the event_id from a prior calendar_list_events call."
            ),
            function=calendar_delete_event,
            params_model=CalendarDeleteEventParams,
            usage_hint=("Delete a calendar event. Confirm with the user first."),
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ASK,
                resource_extractor=lambda args: f"delete {args.get('event_id', '')}",
                description_builder=lambda args: (
                    f"Delete calendar event {args.get('event_id', '')}"
                ),
            ),
        ),
        Tool(
            name=ToolName.CALENDAR_CHECK_AVAILABILITY,
            description=(
                "Check free/busy status on Google Calendar for a date range. "
                "Returns busy time slots. Use this before suggesting new "
                "appointment times."
            ),
            function=calendar_check_availability,
            params_model=CalendarCheckAvailabilityParams,
            usage_hint=(
                "Check availability before scheduling. Always use this before creating events."
            ),
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.AUTO,
            ),
        ),
    ]


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def _handle_http_error(exc: httpx.HTTPStatusError, action: str) -> ToolResult:
    """Convert an HTTP error into a user-friendly ToolResult."""
    status = exc.response.status_code
    if status == 401:
        return ToolResult(
            content="Calendar disconnected. Please reconnect Google Calendar in Settings.",
            is_error=True,
            error_kind=ToolErrorKind.SERVICE,
        )
    if status == 404:
        return ToolResult(
            content=f"Calendar event not found while trying to {action}.",
            is_error=True,
            error_kind=ToolErrorKind.NOT_FOUND,
        )
    if status == 429:
        return ToolResult(
            content="Calendar rate limited. Try again shortly.",
            is_error=True,
            error_kind=ToolErrorKind.SERVICE,
            hint="Wait a moment before retrying calendar operations.",
        )
    logger.exception("Calendar HTTP error during %s", action)
    return ToolResult(
        content=f"Calendar service error ({status}) while trying to {action}.",
        is_error=True,
        error_kind=ToolErrorKind.SERVICE,
    )


# ---------------------------------------------------------------------------
# Factory and registration
# ---------------------------------------------------------------------------


def _calendar_factory(ctx: ToolContext) -> list[Tool]:
    """Factory for calendar tools, used by the registry."""
    if not settings.google_calendar_client_id or not settings.google_calendar_client_secret:
        return []
    token = oauth_service.load_token(ctx.user.id, "google_calendar")
    if token is None or not token.access_token:
        return []
    service = GoogleCalendarService(
        access_token=token.access_token,
        refresh_token=token.refresh_token,
        client_id=settings.google_calendar_client_id,
        client_secret=settings.google_calendar_client_secret,
        on_token_refresh=_make_token_refresh_callback(ctx.user.id),
        token_expires_at=token.expires_at,
    )
    return create_calendar_tools(service)


def _register() -> None:
    from backend.app.agent.tools.registry import SubToolInfo, default_registry

    default_registry.register(
        "calendar",
        _calendar_factory,
        core=False,
        summary=("Read and manage Google Calendar events, check availability"),
        sub_tools=[
            SubToolInfo(
                ToolName.CALENDAR_LIST_EVENTS,
                "List calendar events in a date range",
            ),
            SubToolInfo(
                ToolName.CALENDAR_CREATE_EVENT,
                "Create a new calendar event",
            ),
            SubToolInfo(
                ToolName.CALENDAR_UPDATE_EVENT,
                "Update an existing calendar event",
            ),
            SubToolInfo(
                ToolName.CALENDAR_DELETE_EVENT,
                "Delete a calendar event",
            ),
            SubToolInfo(
                ToolName.CALENDAR_CHECK_AVAILABILITY,
                "Check calendar free/busy availability",
            ),
        ],
    )


_register()
