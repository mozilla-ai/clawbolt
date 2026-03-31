"""Google Calendar tools for the agent."""

from __future__ import annotations

import contextlib
import json
import logging
import time
import zoneinfo
from datetime import UTC, datetime, tzinfo
from typing import TYPE_CHECKING, Any

import httpx
from pydantic import BaseModel, Field

from backend.app.agent.approval import ApprovalPolicy, PermissionLevel
from backend.app.agent.tools.base import Tool, ToolErrorKind, ToolResult
from backend.app.agent.tools.names import ToolName
from backend.app.config import settings
from backend.app.database import SessionLocal
from backend.app.models import CalendarConfig
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
# Shared helpers
# ---------------------------------------------------------------------------


def parse_disabled_tools(raw: str) -> list[str]:
    """Parse a JSON list of disabled tool names, falling back to [].

    Used by both the calendar router and the tool factory to deserialise
    the ``CalendarConfig.disabled_tools`` column.
    """
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(x) for x in parsed]
    except (json.JSONDecodeError, TypeError):
        pass
    return []


# ---------------------------------------------------------------------------
# Param models
# ---------------------------------------------------------------------------


class CalendarListCalendarsParams(BaseModel):
    """Parameters for the calendar_list_calendars tool."""


class CalendarListEventsParams(BaseModel):
    """Parameters for the calendar_list_events tool."""

    start_date: str = Field(
        description=("Start of the time range in ISO 8601 format (e.g. 2026-03-23T00:00:00).")
    )
    end_date: str = Field(
        description=("End of the time range in ISO 8601 format (e.g. 2026-03-30T23:59:59).")
    )
    calendar_id: str = Field(
        default="",
        description=(
            "Calendar ID to query. Leave empty to query all enabled calendars. "
            "Specify a calendar ID to query only that calendar."
        ),
    )


class CalendarCreateEventParams(BaseModel):
    """Parameters for the calendar_create_event tool."""

    title: str = Field(description="Event title (e.g. 'Job: Smith Kitchen Remodel').")
    start: str = Field(description="Event start in ISO 8601 format (e.g. 2026-03-25T09:00:00).")
    end: str = Field(description="Event end in ISO 8601 format (e.g. 2026-03-25T17:00:00).")
    description: str = Field(default="", description="Event description or notes.")
    location: str = Field(default="", description="Event location address.")
    calendar_id: str = Field(
        default="",
        description=(
            "Calendar ID to create the event on. Required when multiple calendars are enabled."
        ),
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
        default="",
        description=("Calendar ID. Required when multiple calendars are enabled."),
    )


class CalendarDeleteEventParams(BaseModel):
    """Parameters for the calendar_delete_event tool."""

    event_id: str = Field(description="Google Calendar event ID to delete.")
    calendar_id: str = Field(
        default="",
        description=("Calendar ID. Required when multiple calendars are enabled."),
    )


class CalendarCheckAvailabilityParams(BaseModel):
    """Parameters for the calendar_check_availability tool."""

    start_date: str = Field(description="Start of the range in ISO 8601 format.")
    end_date: str = Field(description="End of the range in ISO 8601 format.")
    calendar_id: str = Field(
        default="",
        description=("Calendar ID to check. Leave empty to check all enabled calendars."),
    )


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _format_event(event: Any, calendar_label: str = "") -> str:
    """Format a single calendar event for LLM readability."""
    parts = []
    if calendar_label:
        parts.append(f"[{calendar_label}]")
    parts.append(event.title)
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


def _resolve_tz(tz_name: str) -> tzinfo:
    """Resolve an IANA timezone name, falling back to UTC."""
    if not tz_name:
        return UTC
    try:
        return zoneinfo.ZoneInfo(tz_name)
    except (zoneinfo.ZoneInfoNotFoundError, KeyError, ValueError):
        logger.warning("Invalid timezone %r, falling back to UTC", tz_name)
        return UTC


def _parse_dt(value: str, default_tz: tzinfo = UTC) -> datetime:
    """Parse an ISO 8601 datetime string.

    If the string has no timezone offset, *default_tz* is used instead of
    blindly assuming UTC.  This lets callers pass the user's local timezone
    so that ``2026-03-24T09:00:00`` is interpreted as 9 AM in the user's
    timezone rather than 9 AM UTC.
    """
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=default_tz)
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
# Multi-calendar helpers
# ---------------------------------------------------------------------------


def _validate_calendar_id(
    calendar_id: str,
    enabled_calendars: list[tuple[str, str, list[str], str]],
    tool_name: str = "",
) -> tuple[str, str | None]:
    """Resolve and validate a calendar_id against the enabled set.

    Returns ``(resolved_id, error_message)``.  When *error_message* is
    ``None`` the *resolved_id* is safe to use.

    Each entry in *enabled_calendars* is
    ``(calendar_id, display_name, disabled_tools, access_role)``.

    When *tool_name* is set, also checks that the tool is not disabled
    on the resolved calendar.
    """
    enabled_ids = {cid for cid, _, _, _ in enabled_calendars}

    # Filter to calendars where this tool is allowed
    if tool_name:
        allowed = [
            (cid, name) for cid, name, disabled, _ in enabled_calendars if tool_name not in disabled
        ]
    else:
        allowed = [(cid, name) for cid, name, _, _ in enabled_calendars]

    if not calendar_id:
        if len(allowed) == 1:
            return allowed[0][0], None
        if not allowed:
            display = tool_name.replace("calendar_", "").replace("_", " ")
            return "", f"No calendars allow {display}. Update calendar permissions in Settings."
        return (
            "",
            f"Multiple calendars available. Please specify calendar_id. Options: {', '.join(f'{name} ({cid})' for cid, name in allowed)}",
        )

    if calendar_id not in enabled_ids:
        return (
            "",
            f"Calendar '{calendar_id}' is not in the enabled set. Options: {', '.join(f'{name} ({cid})' for cid, name, _, _ in enabled_calendars)}",
        )

    if tool_name:
        for cid, name, disabled, role in enabled_calendars:
            if cid == calendar_id and tool_name in disabled:
                display = tool_name.replace("calendar_", "").replace("_", " ")
                reason = " (read-only calendar)" if role in _READ_ONLY_ROLES else ""
                return (
                    "",
                    f"'{name}' does not allow {display}{reason}. "
                    "Update calendar permissions in Settings.",
                )

    return calendar_id, None


# Per-calendar tools (can be disabled individually per calendar).
# Global tools (list_calendars, check_availability) are controlled by the
# existing sub-tool toggle system and are not per-calendar.
_PER_CALENDAR_TOOLS = [
    ToolName.CALENDAR_LIST_EVENTS,
    ToolName.CALENDAR_CREATE_EVENT,
    ToolName.CALENDAR_UPDATE_EVENT,
    ToolName.CALENDAR_DELETE_EVENT,
]

# Write tools that should be auto-disabled on read-only calendars.
_WRITE_TOOLS = [
    ToolName.CALENDAR_CREATE_EVENT,
    ToolName.CALENDAR_UPDATE_EVENT,
    ToolName.CALENDAR_DELETE_EVENT,
]

# Google Calendar access roles that only allow reading.
_READ_ONLY_ROLES = {"reader", "freeBusyReader"}


# ---------------------------------------------------------------------------
# Tool creation
# ---------------------------------------------------------------------------


def create_calendar_tools(
    service: GoogleCalendarService | Any,
    user_timezone: str = "",
    enabled_calendars: list[tuple[str, str, list[str], str]] | None = None,
) -> list[Tool]:
    """Create calendar tools bound to a calendar service instance.

    *user_timezone* is an IANA timezone name (e.g. ``America/New_York``).
    When the LLM omits a timezone offset from date strings, this timezone
    is used instead of UTC so that ``09:00`` means 9 AM in the user's
    local time.

    *enabled_calendars* is a list of
    ``(calendar_id, display_name, disabled_tools, access_role)`` tuples.
    *disabled_tools* is a list of tool names disabled on that calendar.
    *access_role* is the Google Calendar access role (owner/writer/reader/freeBusyReader).
    When empty or ``None``, defaults to ``[("primary", "Primary", [], "owner")]``.
    """
    default_tz = _resolve_tz(user_timezone)
    raw: list[tuple[str, str, list[str], str]] = enabled_calendars or [
        ("primary", "Primary", [], "owner")
    ]
    # Auto-block write tools on read-only calendars.
    _enabled: list[tuple[str, str, list[str], str]] = []
    for cid, name, disabled, role in raw:
        if role in _READ_ONLY_ROLES:
            disabled_set = set(disabled)
            merged = list(disabled)
            for wt in _WRITE_TOOLS:
                if wt not in disabled_set:
                    merged.append(wt)
            _enabled.append((cid, name, merged, role))
        else:
            _enabled.append((cid, name, disabled, role))
    _enabled_ids = {cid for cid, _, _, _ in _enabled}
    _cal_name_map = {cid: name for cid, name, _, _ in _enabled}
    _disabled_map = {cid: set(disabled) for cid, _, disabled, _ in _enabled}

    async def calendar_list_calendars() -> ToolResult:
        """List enabled calendars for the user."""
        if not _enabled:
            return ToolResult(content="No calendars enabled.")

        lines = [f"{len(_enabled)} enabled calendar(s):"]
        for cal_id, cal_name, disabled, access_role in _enabled:
            disabled_set = set(disabled)
            allowed = [
                t.replace("calendar_", "").replace("_", " ")
                for t in _PER_CALENDAR_TOOLS
                if t not in disabled_set
            ]
            blocked = [
                t.replace("calendar_", "").replace("_", " ")
                for t in _PER_CALENDAR_TOOLS
                if t in disabled_set
            ]
            parts = [cal_name]
            if access_role:
                parts.append(f"access: {access_role}")
            parts.append(f"allowed: {', '.join(allowed)}" if allowed else "allowed: none")
            if blocked:
                parts.append(f"blocked: {', '.join(blocked)}")
            parts.append(f"id: {cal_id}")
            lines.append("- " + " | ".join(parts))
        return ToolResult(content="\n".join(lines))

    async def calendar_list_events(
        start_date: str, end_date: str, calendar_id: str = ""
    ) -> ToolResult:
        """List calendar events in a time range."""
        logger.debug(
            "list_events called: start=%s end=%s calendar_id=%r enabled=%s",
            start_date,
            end_date,
            calendar_id,
            [(cid, name) for cid, name, _, _ in _enabled],
        )
        try:
            time_min = _parse_dt(start_date, default_tz)
            time_max = _parse_dt(end_date, default_tz)
        except ValueError as exc:
            return ToolResult(
                content=f"Invalid date format: {exc}. Use ISO 8601 (e.g. 2026-03-23T00:00:00).",
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
            )

        # Determine which calendars to query
        tool = ToolName.CALENDAR_LIST_EVENTS
        if calendar_id:
            resolved_id, err = _validate_calendar_id(calendar_id, _enabled, tool_name=tool)
            if err:
                return ToolResult(content=err, is_error=True, error_kind=ToolErrorKind.VALIDATION)
            query_cals = [(resolved_id, _cal_name_map[resolved_id])]
        else:
            # Query all calendars where list_events is not disabled
            query_cals = [
                (cid, name)
                for cid, name, _, _ in _enabled
                if tool not in _disabled_map.get(cid, set())
            ]

        logger.debug("list_events querying calendars: %s", query_cals)
        all_events: list[tuple[str, Any]] = []
        skipped: list[str] = []
        for cal_id, cal_name in query_cals:
            try:
                events = await service.list_events(cal_id, time_min, time_max)
                for event in events:
                    all_events.append((cal_name, event))
            except httpx.TimeoutException:
                return ToolResult(
                    content="Calendar service unavailable (timeout). Try again shortly.",
                    is_error=True,
                    error_kind=ToolErrorKind.SERVICE,
                )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404 and len(query_cals) > 1:
                    logger.warning("Calendar %s (%s) returned 404, skipping", cal_id, cal_name)
                    skipped.append(cal_name)
                    continue
                return _handle_http_error(exc, "list events")
            except Exception as exc:
                logger.exception("Calendar list_events failed for %s", cal_id)
                return ToolResult(
                    content=f"Calendar error: {exc}",
                    is_error=True,
                    error_kind=ToolErrorKind.SERVICE,
                )

        skip_note = ""
        if skipped:
            skip_note = (
                f"\n(Skipped {len(skipped)} calendar(s) not found: "
                f"{', '.join(skipped)}. Refresh calendar config in Settings.)"
            )

        if not all_events:
            return ToolResult(
                content=f"No events found between {start_date} and {end_date}.{skip_note}"
            )

        # Sort by event start time
        all_events.sort(key=lambda pair: pair[1].start)

        show_label = len(query_cals) > 1
        lines = [f"Found {len(all_events)} event(s):"]
        for cal_name, event in all_events:
            lines.append(f"- {_format_event(event, calendar_label=cal_name if show_label else '')}")
        if skip_note:
            lines.append(skip_note)
        return ToolResult(content="\n".join(lines))

    async def calendar_create_event(
        title: str,
        start: str,
        end: str,
        description: str = "",
        location: str = "",
        calendar_id: str = "",
    ) -> ToolResult:
        """Create a new calendar event."""
        logger.debug("create_event called: title=%r calendar_id=%r", title, calendar_id)
        resolved_id, err = _validate_calendar_id(
            calendar_id, _enabled, tool_name=ToolName.CALENDAR_CREATE_EVENT
        )
        if err:
            return ToolResult(content=err, is_error=True, error_kind=ToolErrorKind.VALIDATION)

        try:
            start_dt = _parse_dt(start, default_tz)
            end_dt = _parse_dt(end, default_tz)
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
            event = await service.create_event(resolved_id, create_data)
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
        calendar_id: str = "",
    ) -> ToolResult:
        """Update an existing calendar event."""
        logger.debug("update_event called: event_id=%r calendar_id=%r", event_id, calendar_id)
        resolved_id, err = _validate_calendar_id(
            calendar_id, _enabled, tool_name=ToolName.CALENDAR_UPDATE_EVENT
        )
        if err:
            return ToolResult(content=err, is_error=True, error_kind=ToolErrorKind.VALIDATION)

        start_dt = None
        end_dt = None
        if start is not None:
            try:
                start_dt = _parse_dt(start, default_tz)
            except ValueError as exc:
                return ToolResult(
                    content=f"Invalid start date: {exc}. Use ISO 8601.",
                    is_error=True,
                    error_kind=ToolErrorKind.VALIDATION,
                )
        if end is not None:
            try:
                end_dt = _parse_dt(end, default_tz)
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
            event = await service.update_event(resolved_id, event_id, updates)
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
        calendar_id: str = "",
    ) -> ToolResult:
        """Delete a calendar event."""
        logger.debug("delete_event called: event_id=%r calendar_id=%r", event_id, calendar_id)
        resolved_id, err = _validate_calendar_id(
            calendar_id, _enabled, tool_name=ToolName.CALENDAR_DELETE_EVENT
        )
        if err:
            return ToolResult(content=err, is_error=True, error_kind=ToolErrorKind.VALIDATION)

        try:
            await service.delete_event(resolved_id, event_id)
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
        start_date: str, end_date: str, calendar_id: str = ""
    ) -> ToolResult:
        """Check calendar availability (free/busy) in a time range."""
        logger.debug(
            "check_availability called: start=%s end=%s calendar_id=%r",
            start_date,
            end_date,
            calendar_id,
        )
        try:
            time_min = _parse_dt(start_date, default_tz)
            time_max = _parse_dt(end_date, default_tz)
        except ValueError as exc:
            return ToolResult(
                content=f"Invalid date format: {exc}. Use ISO 8601.",
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
            )

        # Determine which calendars to check (availability is global)
        if calendar_id:
            if calendar_id not in _enabled_ids:
                return ToolResult(
                    content=f"Calendar '{calendar_id}' is not in the enabled set. Options: {', '.join(f'{name} ({cid})' for cid, name, _, _ in _enabled)}",
                    is_error=True,
                    error_kind=ToolErrorKind.VALIDATION,
                )
            query_cals = [(calendar_id, _cal_name_map[calendar_id])]
        else:
            query_cals = [(cid, name) for cid, name, _, _ in _enabled]

        all_slots: list[tuple[str, Any]] = []
        skipped: list[str] = []
        for cal_id, cal_name in query_cals:
            try:
                busy_slots = await service.check_availability(cal_id, time_min, time_max)
                for slot in busy_slots:
                    all_slots.append((cal_name, slot))
            except httpx.TimeoutException:
                return ToolResult(
                    content="Calendar service unavailable (timeout). Try again shortly.",
                    is_error=True,
                    error_kind=ToolErrorKind.SERVICE,
                )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404 and len(query_cals) > 1:
                    logger.warning("Calendar %s (%s) returned 404, skipping", cal_id, cal_name)
                    skipped.append(cal_name)
                    continue
                return _handle_http_error(exc, "check availability")
            except Exception as exc:
                logger.exception("Calendar check_availability failed for %s", cal_id)
                return ToolResult(
                    content=f"Calendar error: {exc}",
                    is_error=True,
                    error_kind=ToolErrorKind.SERVICE,
                )

        skip_note = ""
        if skipped:
            skip_note = (
                f"\n(Skipped {len(skipped)} calendar(s) not found: "
                f"{', '.join(skipped)}. Refresh calendar config in Settings.)"
            )

        if not all_slots:
            return ToolResult(
                content=f"Calendar is free between {start_date} and {end_date}.{skip_note}"
            )

        all_slots.sort(key=lambda pair: pair[1].start)

        show_label = len(query_cals) > 1
        lines = [f"Found {len(all_slots)} busy slot(s):"]
        for cal_name, slot in all_slots:
            label = f"[{cal_name}] " if show_label else ""
            lines.append(
                f"- {label}{slot.start.strftime('%Y-%m-%d %H:%M')} - {slot.end.strftime('%H:%M')}"
            )
        return ToolResult(content="\n".join(lines))

    return [
        Tool(
            name=ToolName.CALENDAR_LIST_CALENDARS,
            description=(
                "List the calendars the user has enabled for the assistant. "
                "Shows calendar names and IDs."
            ),
            function=calendar_list_calendars,
            params_model=CalendarListCalendarsParams,
            usage_hint=("List enabled calendars to help the user pick the right one."),
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.AUTO,
            ),
        ),
        Tool(
            name=ToolName.CALENDAR_LIST_EVENTS,
            description=(
                "List events on Google Calendar within a date range. "
                "Returns event titles, times, locations, and IDs. "
                "When no calendar_id is specified, queries all enabled calendars."
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
                "IMPORTANT: Some calendars are read-only. Check calendar_list_calendars "
                "first to verify the target calendar allows creation. "
                "Use 'Job: {client} - {description}' format for job events. "
                "Include the job location. "
                "Specify calendar_id when multiple calendars are enabled."
            ),
            function=calendar_create_event,
            params_model=CalendarCreateEventParams,
            usage_hint=(
                "Create a calendar event. Check calendar_list_calendars for write access "
                "and availability first. Use 'Job: Client - Description' format for job titles."
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
                "Only works on calendars with update access (check calendar_list_calendars). "
                "Pass the event_id from a prior calendar_list_events call "
                "and only the fields to change."
            ),
            function=calendar_update_event,
            params_model=CalendarUpdateEventParams,
            usage_hint=(
                "Update an existing event. Verify the calendar allows updates first. "
                "Get the event_id from calendar_list_events."
            ),
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ASK,
                resource_extractor=lambda args: f"update {args.get('event_id', '')}",
                description_builder=lambda args: (
                    f"Update calendar event: {args['title']}"
                    if args.get("title")
                    else "Update a calendar event"
                ),
            ),
        ),
        Tool(
            name=ToolName.CALENDAR_DELETE_EVENT,
            description=(
                "Delete an event from Google Calendar. "
                "Only works on calendars with delete access (check calendar_list_calendars). "
                "Pass the event_id from a prior calendar_list_events call."
            ),
            function=calendar_delete_event,
            params_model=CalendarDeleteEventParams,
            usage_hint=(
                "Delete a calendar event. Verify the calendar allows deletion first. "
                "Confirm with the user before deleting."
            ),
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ASK,
                resource_extractor=lambda args: f"delete {args.get('event_id', '')}",
                description_builder=lambda args: "Delete a calendar event",
            ),
        ),
        Tool(
            name=ToolName.CALENDAR_CHECK_AVAILABILITY,
            description=(
                "Check free/busy status on Google Calendar for a date range. "
                "Returns busy time slots. Use this before suggesting new "
                "appointment times. "
                "When no calendar_id is specified, checks all enabled calendars."
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
    body = ""
    with contextlib.suppress(Exception):
        body = exc.response.text[:500]
    logger.warning(
        "Calendar HTTP %d during %s: url=%s body=%s",
        status,
        action,
        str(exc.request.url) if exc.request else "unknown",
        body,
    )
    if status == 401:
        return ToolResult(
            content="Calendar disconnected. Please reconnect Google Calendar in Settings.",
            is_error=True,
            error_kind=ToolErrorKind.SERVICE,
        )
    if status == 403:
        return ToolResult(
            content=(
                f"Permission denied while trying to {action}. "
                "This calendar is likely read-only. "
                "Use calendar_list_calendars to check which calendars allow writes."
            ),
            is_error=True,
            error_kind=ToolErrorKind.VALIDATION,
        )
    if status == 404:
        return ToolResult(
            content=f"Not found while trying to {action}. The calendar or event may no longer exist.",
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
    return ToolResult(
        content=f"Calendar service error ({status}) while trying to {action}.",
        is_error=True,
        error_kind=ToolErrorKind.SERVICE,
    )


# ---------------------------------------------------------------------------
# Factory and registration
# ---------------------------------------------------------------------------


def _calendar_auth_check(ctx: ToolContext) -> str | None:
    """Check whether Google Calendar is configured and the user has authenticated.

    Returns ``None`` when ready, or a reason string when auth is missing.
    Returns ``None`` (not a reason) when the integration is not configured
    at all (admin has not set credentials), so it stays completely hidden.
    """
    if not settings.google_calendar_client_id or not settings.google_calendar_client_secret:
        return None
    token = oauth_service.load_token(ctx.user.id, "google_calendar")
    if token is not None and token.access_token:
        return None
    return (
        "Google Calendar is not connected. "
        "The user needs to authenticate via the Clawbolt web dashboard."
    )


def _get_enabled_calendars(user_id: str) -> list[tuple[str, str, list[str], str]]:
    """Load the user's enabled calendars from CalendarConfig.

    Returns a list of ``(calendar_id, display_name, disabled_tools, access_role)``
    tuples.  Write tools are automatically added to *disabled_tools* for
    calendars whose *access_role* is ``reader`` or ``freeBusyReader``.
    """
    db = SessionLocal()
    try:
        configs = (
            db.query(CalendarConfig).filter_by(user_id=user_id, provider="google_calendar").all()
        )
        if configs:
            result: list[tuple[str, str, list[str], str]] = []
            for c in configs:
                disabled = parse_disabled_tools(c.disabled_tools)
                role = c.access_role or ""
                # Auto-block write tools on read-only calendars
                if role in _READ_ONLY_ROLES:
                    disabled_set = set(disabled)
                    for wt in _WRITE_TOOLS:
                        if wt not in disabled_set:
                            disabled.append(wt)
                result.append(
                    (
                        c.calendar_id,
                        c.display_name or c.calendar_id,
                        disabled,
                        role,
                    )
                )
            logger.debug(
                "Loaded %d enabled calendar(s) for user %s: %s",
                len(result),
                user_id,
                [(cid, name, role) for cid, name, _, role in result],
            )
            return result
    finally:
        db.close()
    logger.debug("No calendar config for user %s, defaulting to primary", user_id)
    return [("primary", "Primary", [], "owner")]


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
    enabled_calendars = _get_enabled_calendars(ctx.user.id)
    return create_calendar_tools(
        service,
        user_timezone=ctx.user.timezone,
        enabled_calendars=enabled_calendars,
    )


def _register() -> None:
    from backend.app.agent.tools.registry import SubToolInfo, default_registry

    default_registry.register(
        "calendar",
        _calendar_factory,
        core=False,
        summary=("Read and manage Google Calendar events, check availability"),
        sub_tools=[
            SubToolInfo(
                ToolName.CALENDAR_LIST_CALENDARS,
                "List available calendars",
            ),
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
        auth_check=_calendar_auth_check,
    )


_register()
