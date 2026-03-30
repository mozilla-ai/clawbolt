"""Google Calendar REST API client using httpx.

Follows the same patterns as quickbooks_service.py: token refresh callback,
reactive 401 retry, and no dependency on google-api-python-client.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx

from backend.app.services.calendar_provider import (
    BusySlot,
    CalendarEventCreate,
    CalendarEventData,
    CalendarEventUpdate,
    CalendarInfo,
)

logger = logging.getLogger(__name__)

GOOGLE_CALENDAR_API_BASE = "https://www.googleapis.com/calendar/v3"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

# Refresh 5 minutes before expiry.
_REFRESH_BUFFER_SECONDS = 300


def _encode_cal_id(calendar_id: str) -> str:
    """URL-encode a calendar ID for use in API paths.

    Google Calendar IDs can contain ``#`` and ``@`` which must be
    percent-encoded when embedded in URL paths (e.g.
    ``en.usa#holiday@group.v.calendar.google.com``).
    """
    return quote(calendar_id, safe="")


class GoogleCalendarService:
    """Google Calendar REST API client."""

    def __init__(
        self,
        access_token: str,
        refresh_token: str,
        client_id: str,
        client_secret: str,
        on_token_refresh: Callable[[str, str], None] | None = None,
        token_expires_at: float = 0.0,
    ) -> None:
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._client_id = client_id
        self._client_secret = client_secret
        self._on_token_refresh = on_token_refresh
        self._token_expires_at = token_expires_at

    @property
    def provider_name(self) -> str:
        return "google_calendar"

    async def _refresh_access_token(self, client: httpx.AsyncClient) -> None:
        """Refresh the OAuth2 access token using the refresh token."""
        logger.info("Refreshing Google Calendar access token")
        resp = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        self._access_token = data["access_token"]
        if "refresh_token" in data:
            self._refresh_token = data["refresh_token"]
        if "expires_in" in data:
            self._token_expires_at = time.time() + data["expires_in"]
        if self._on_token_refresh:
            self._on_token_refresh(self._access_token, self._refresh_token)

    async def _ensure_valid_token(self, client: httpx.AsyncClient) -> None:
        """Proactively refresh the token if it is about to expire."""
        if self._token_expires_at <= 0:
            return
        if time.time() >= (self._token_expires_at - _REFRESH_BUFFER_SECONDS):
            await self._refresh_access_token(client)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any] | None:
        """Make an authenticated request to the Google Calendar API.

        Returns the parsed JSON body, or None for 204 responses.
        Automatically refreshes the token on 401.
        """
        url = f"{GOOGLE_CALENDAR_API_BASE}{path}"
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            await self._ensure_valid_token(client)
            headers["Authorization"] = f"Bearer {self._access_token}"

            resp = await client.request(method, url, headers=headers, json=json, params=params)

            if resp.status_code == 401:
                await self._refresh_access_token(client)
                headers["Authorization"] = f"Bearer {self._access_token}"
                resp = await client.request(method, url, headers=headers, json=json, params=params)

            resp.raise_for_status()

            if resp.status_code == 204:
                return None
            return resp.json()

    # -- Public API -----------------------------------------------------------

    async def list_calendars(self) -> list[CalendarInfo]:
        """List calendars visible to the authenticated user."""
        data = await self._request("GET", "/users/me/calendarList")
        items = (data or {}).get("items", [])
        return [
            CalendarInfo(
                id=item.get("id", ""),
                summary=item.get("summary", ""),
                primary=item.get("primary", False),
            )
            for item in items
        ]

    async def list_events(
        self,
        calendar_id: str,
        time_min: datetime,
        time_max: datetime,
    ) -> list[CalendarEventData]:
        """List events in a calendar within a time range."""
        params = {
            "timeMin": _to_rfc3339(time_min),
            "timeMax": _to_rfc3339(time_max),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": "250",
        }
        data = await self._request(
            "GET", f"/calendars/{_encode_cal_id(calendar_id)}/events", params=params
        )
        items = (data or {}).get("items", [])
        events: list[CalendarEventData] = []
        for item in items:
            try:
                events.append(_parse_event(item))
            except (KeyError, ValueError) as exc:
                logger.warning("Skipping malformed calendar event %s: %s", item.get("id"), exc)
        return events

    async def create_event(
        self,
        calendar_id: str,
        event: CalendarEventCreate,
    ) -> CalendarEventData:
        """Create a new event on a calendar."""
        body = _build_event_body(event)
        data = await self._request(
            "POST", f"/calendars/{_encode_cal_id(calendar_id)}/events", json=body
        )
        return _parse_event(data or {})

    async def update_event(
        self,
        calendar_id: str,
        event_id: str,
        updates: CalendarEventUpdate,
    ) -> CalendarEventData:
        """Update an existing event (PATCH semantics)."""
        body: dict[str, Any] = {}
        if updates.title is not None:
            body["summary"] = updates.title
        if updates.description is not None:
            body["description"] = updates.description
        if updates.location is not None:
            body["location"] = updates.location
        if updates.start is not None:
            body["start"] = {"dateTime": _to_rfc3339(updates.start)}
        if updates.end is not None:
            body["end"] = {"dateTime": _to_rfc3339(updates.end)}

        data = await self._request(
            "PATCH",
            f"/calendars/{_encode_cal_id(calendar_id)}/events/{event_id}",
            json=body,
        )
        return _parse_event(data or {})

    async def delete_event(
        self,
        calendar_id: str,
        event_id: str,
    ) -> None:
        """Delete an event from a calendar."""
        await self._request("DELETE", f"/calendars/{_encode_cal_id(calendar_id)}/events/{event_id}")

    async def check_availability(
        self,
        calendar_id: str,
        time_min: datetime,
        time_max: datetime,
    ) -> list[BusySlot]:
        """Check free/busy information for a calendar."""
        body = {
            "timeMin": _to_rfc3339(time_min),
            "timeMax": _to_rfc3339(time_max),
            "items": [{"id": calendar_id}],
        }
        data = await self._request("POST", "/freeBusy", json=body)
        calendars = (data or {}).get("calendars", {})
        # Google may return the resolved email as key instead of "primary",
        # so collect busy slots from all calendars in the response (we only
        # requested one).
        busy_list: list[dict[str, str]] = []
        for cal_data in calendars.values():
            busy_list.extend(cal_data.get("busy", []))
        return [
            BusySlot(
                start=datetime.fromisoformat(slot["start"]),
                end=datetime.fromisoformat(slot["end"]),
            )
            for slot in busy_list
        ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_rfc3339(dt: datetime) -> str:
    """Convert a datetime to RFC 3339 format for the Google API."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat()


def _parse_event(item: dict[str, Any]) -> CalendarEventData:
    """Parse a Google Calendar API event item into a CalendarEventData."""
    start_raw = item.get("start", {})
    end_raw = item.get("end", {})

    all_day = "date" in start_raw and "dateTime" not in start_raw

    if all_day:
        start = datetime.fromisoformat(start_raw["date"]).replace(tzinfo=UTC)
        end = datetime.fromisoformat(end_raw.get("date", start_raw["date"])).replace(tzinfo=UTC)
    else:
        start = datetime.fromisoformat(start_raw.get("dateTime", ""))
        end = datetime.fromisoformat(end_raw.get("dateTime", ""))

    return CalendarEventData(
        id=item.get("id", ""),
        title=item.get("summary", "(No title)"),
        description=item.get("description", ""),
        start=start,
        end=end,
        location=item.get("location", ""),
        all_day=all_day,
        status=item.get("status", "confirmed"),
    )


def _build_event_body(event: CalendarEventCreate) -> dict[str, Any]:
    """Build a Google Calendar API event body from a CalendarEventCreate DTO."""
    body: dict[str, Any] = {
        "summary": event.title,
    }
    if event.description:
        body["description"] = event.description
    if event.location:
        body["location"] = event.location

    if event.all_day:
        body["start"] = {"date": event.start.strftime("%Y-%m-%d")}
        body["end"] = {"date": event.end.strftime("%Y-%m-%d")}
    else:
        body["start"] = {"dateTime": _to_rfc3339(event.start)}
        body["end"] = {"dateTime": _to_rfc3339(event.end)}

    return body
