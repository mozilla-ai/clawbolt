"""Tests for GoogleCalendarService."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from backend.app.services.calendar_provider import (
    CalendarEventCreate,
    CalendarEventUpdate,
)
from backend.app.services.google_calendar import GoogleCalendarService


def _mock_response(
    status_code: int = 200,
    json_data: dict[str, Any] | None = None,
) -> httpx.Response:
    """Create a mock httpx response."""
    return httpx.Response(
        status_code,
        json=json_data or {},
        request=httpx.Request("GET", "https://example.com"),
    )


@pytest.fixture()
def service() -> GoogleCalendarService:
    return GoogleCalendarService(
        access_token="test-access-token",
        refresh_token="test-refresh-token",
        client_id="test-client-id",
        client_secret="test-client-secret",
        token_expires_at=time.time() + 3600,
    )


# ---------------------------------------------------------------------------
# list_events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_list_events_returns_events(service: GoogleCalendarService) -> None:
    """Should parse Google Calendar event items into CalendarEventData."""
    api_response = {
        "items": [
            {
                "id": "abc123",
                "summary": "Job: Smith Remodel",
                "description": "Kitchen work",
                "start": {"dateTime": "2026-03-25T09:00:00+00:00"},
                "end": {"dateTime": "2026-03-25T17:00:00+00:00"},
                "location": "123 Oak St",
                "status": "confirmed",
            }
        ]
    }

    with patch.object(service, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = api_response
        events = await service.list_events(
            "primary",
            datetime(2026, 3, 25, tzinfo=UTC),
            datetime(2026, 3, 26, tzinfo=UTC),
        )

    assert len(events) == 1
    assert events[0].id == "abc123"
    assert events[0].title == "Job: Smith Remodel"
    assert events[0].location == "123 Oak St"


@pytest.mark.asyncio()
async def test_list_events_empty(service: GoogleCalendarService) -> None:
    """Should return empty list when no events."""
    with patch.object(service, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = {"items": []}
        events = await service.list_events(
            "primary",
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 2, tzinfo=UTC),
        )

    assert events == []


@pytest.mark.asyncio()
async def test_list_events_skips_malformed_events(service: GoogleCalendarService) -> None:
    """Should skip malformed events instead of crashing the entire list."""
    api_response = {
        "items": [
            {
                "id": "good-event",
                "summary": "Valid Event",
                "start": {"dateTime": "2026-03-25T09:00:00+00:00"},
                "end": {"dateTime": "2026-03-25T17:00:00+00:00"},
                "status": "confirmed",
            },
            {
                "id": "bad-event",
                "summary": "Missing start/end",
                "start": {},
                "end": {},
            },
        ]
    }

    with patch.object(service, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = api_response
        events = await service.list_events(
            "primary",
            datetime(2026, 3, 25, tzinfo=UTC),
            datetime(2026, 3, 26, tzinfo=UTC),
        )

    assert len(events) == 1
    assert events[0].id == "good-event"


@pytest.mark.asyncio()
async def test_list_events_api_error(service: GoogleCalendarService) -> None:
    """Should propagate API errors."""
    with patch.object(service, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.side_effect = httpx.HTTPStatusError(
            "Server Error",
            request=httpx.Request("GET", "https://example.com"),
            response=_mock_response(500),
        )
        with pytest.raises(httpx.HTTPStatusError):
            await service.list_events(
                "primary",
                datetime(2026, 3, 25, tzinfo=UTC),
                datetime(2026, 3, 26, tzinfo=UTC),
            )


# ---------------------------------------------------------------------------
# create_event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_create_event_success(service: GoogleCalendarService) -> None:
    """Should create an event and return parsed data."""
    api_response = {
        "id": "new-event-id",
        "summary": "Job: Test",
        "start": {"dateTime": "2026-03-28T09:00:00+00:00"},
        "end": {"dateTime": "2026-03-28T17:00:00+00:00"},
        "status": "confirmed",
    }

    with patch.object(service, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = api_response
        event = await service.create_event(
            "primary",
            CalendarEventCreate(
                title="Job: Test",
                start=datetime(2026, 3, 28, 9, 0, tzinfo=UTC),
                end=datetime(2026, 3, 28, 17, 0, tzinfo=UTC),
            ),
        )

    assert event.id == "new-event-id"
    assert event.title == "Job: Test"


@pytest.mark.asyncio()
async def test_create_event_api_error(service: GoogleCalendarService) -> None:
    """Should propagate API errors on create."""
    with patch.object(service, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.side_effect = httpx.HTTPStatusError(
            "Bad Request",
            request=httpx.Request("POST", "https://example.com"),
            response=_mock_response(400),
        )
        with pytest.raises(httpx.HTTPStatusError):
            await service.create_event(
                "primary",
                CalendarEventCreate(
                    title="Test",
                    start=datetime(2026, 3, 28, 9, 0, tzinfo=UTC),
                    end=datetime(2026, 3, 28, 17, 0, tzinfo=UTC),
                ),
            )


# ---------------------------------------------------------------------------
# update_event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_update_event_success(service: GoogleCalendarService) -> None:
    """Should update an event and return parsed data."""
    api_response = {
        "id": "evt-001",
        "summary": "Updated Title",
        "start": {"dateTime": "2026-03-25T10:00:00+00:00"},
        "end": {"dateTime": "2026-03-25T18:00:00+00:00"},
        "status": "confirmed",
    }

    with patch.object(service, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = api_response
        event = await service.update_event(
            "primary",
            "evt-001",
            CalendarEventUpdate(title="Updated Title"),
        )

    assert event.title == "Updated Title"
    mock_req.assert_called_once()
    call_args = mock_req.call_args
    assert call_args[0][0] == "PATCH"
    assert "evt-001" in call_args[0][1]


@pytest.mark.asyncio()
async def test_update_event_not_found(service: GoogleCalendarService) -> None:
    """Should raise on 404."""
    with patch.object(service, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.side_effect = httpx.HTTPStatusError(
            "Not Found",
            request=httpx.Request("PATCH", "https://example.com"),
            response=_mock_response(404),
        )
        with pytest.raises(httpx.HTTPStatusError):
            await service.update_event(
                "primary",
                "nonexistent",
                CalendarEventUpdate(title="X"),
            )


# ---------------------------------------------------------------------------
# delete_event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_delete_event_success(service: GoogleCalendarService) -> None:
    """Should delete without error."""
    with patch.object(service, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = None  # 204
        await service.delete_event("primary", "evt-001")

    mock_req.assert_called_once()
    assert "evt-001" in mock_req.call_args[0][1]


@pytest.mark.asyncio()
async def test_delete_event_not_found(service: GoogleCalendarService) -> None:
    """Should raise on 404."""
    with patch.object(service, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.side_effect = httpx.HTTPStatusError(
            "Not Found",
            request=httpx.Request("DELETE", "https://example.com"),
            response=_mock_response(404),
        )
        with pytest.raises(httpx.HTTPStatusError):
            await service.delete_event("primary", "nonexistent")


# ---------------------------------------------------------------------------
# check_availability
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_check_availability_busy(service: GoogleCalendarService) -> None:
    """Should return busy slots."""
    api_response = {
        "calendars": {
            "primary": {
                "busy": [
                    {
                        "start": "2026-03-25T09:00:00+00:00",
                        "end": "2026-03-25T17:00:00+00:00",
                    }
                ]
            }
        }
    }

    with patch.object(service, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = api_response
        slots = await service.check_availability(
            "primary",
            datetime(2026, 3, 25, tzinfo=UTC),
            datetime(2026, 3, 26, tzinfo=UTC),
        )

    assert len(slots) == 1
    assert slots[0].start.hour == 9


@pytest.mark.asyncio()
async def test_check_availability_email_key(service: GoogleCalendarService) -> None:
    """Should find busy slots even when Google returns email as key instead of 'primary'."""
    api_response = {
        "calendars": {
            "user@gmail.com": {
                "busy": [
                    {
                        "start": "2026-03-25T09:00:00+00:00",
                        "end": "2026-03-25T17:00:00+00:00",
                    }
                ]
            }
        }
    }

    with patch.object(service, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = api_response
        slots = await service.check_availability(
            "primary",
            datetime(2026, 3, 25, tzinfo=UTC),
            datetime(2026, 3, 26, tzinfo=UTC),
        )

    assert len(slots) == 1
    assert slots[0].start.hour == 9


@pytest.mark.asyncio()
async def test_check_availability_free(service: GoogleCalendarService) -> None:
    """Should return empty list when free."""
    api_response = {"calendars": {"primary": {"busy": []}}}

    with patch.object(service, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = api_response
        slots = await service.check_availability(
            "primary",
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 2, tzinfo=UTC),
        )

    assert slots == []


@pytest.mark.asyncio()
async def test_check_availability_api_error(service: GoogleCalendarService) -> None:
    """Should propagate API errors."""
    with patch.object(service, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.side_effect = httpx.HTTPStatusError(
            "Error",
            request=httpx.Request("POST", "https://example.com"),
            response=_mock_response(500),
        )
        with pytest.raises(httpx.HTTPStatusError):
            await service.check_availability(
                "primary",
                datetime(2026, 3, 25, tzinfo=UTC),
                datetime(2026, 3, 26, tzinfo=UTC),
            )


# ---------------------------------------------------------------------------
# Token refresh
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_proactive_token_refresh() -> None:
    """Should refresh token when about to expire."""
    svc = GoogleCalendarService(
        access_token="old-token",
        refresh_token="refresh-token",
        client_id="cid",
        client_secret="csec",
        token_expires_at=time.time() - 100,  # Already expired
    )

    refresh_response = httpx.Response(
        200,
        json={
            "access_token": "new-token",
            "refresh_token": "new-refresh",
            "expires_in": 3600,
        },
        request=httpx.Request("POST", "https://oauth2.googleapis.com/token"),
    )
    api_response = httpx.Response(
        200,
        json={"items": []},
        request=httpx.Request(
            "GET", "https://www.googleapis.com/calendar/v3/calendars/primary/events"
        ),
    )

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post.return_value = refresh_response
        mock_client.request.return_value = api_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        events = await svc.list_events(
            "primary",
            datetime(2026, 3, 25, tzinfo=UTC),
            datetime(2026, 3, 26, tzinfo=UTC),
        )

    assert events == []
    # Token was refreshed proactively
    assert svc._access_token == "new-token"


@pytest.mark.asyncio()
async def test_reactive_token_refresh_on_401() -> None:
    """Should refresh and retry on 401."""
    svc = GoogleCalendarService(
        access_token="expired-token",
        refresh_token="refresh-token",
        client_id="cid",
        client_secret="csec",
        token_expires_at=time.time() + 3600,  # Not yet expired (but server rejects)
    )

    refresh_response = httpx.Response(
        200,
        json={
            "access_token": "fresh-token",
            "expires_in": 3600,
        },
        request=httpx.Request("POST", "https://oauth2.googleapis.com/token"),
    )
    unauthorized_response = httpx.Response(
        401,
        json={"error": "invalid_token"},
        request=httpx.Request(
            "GET", "https://www.googleapis.com/calendar/v3/calendars/primary/events"
        ),
    )
    success_response = httpx.Response(
        200,
        json={"items": []},
        request=httpx.Request(
            "GET", "https://www.googleapis.com/calendar/v3/calendars/primary/events"
        ),
    )

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post.return_value = refresh_response
        mock_client.request.side_effect = [unauthorized_response, success_response]
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        events = await svc.list_events(
            "primary",
            datetime(2026, 3, 25, tzinfo=UTC),
            datetime(2026, 3, 26, tzinfo=UTC),
        )

    assert events == []
    assert svc._access_token == "fresh-token"


@pytest.mark.asyncio()
async def test_token_refresh_callback() -> None:
    """Should call on_token_refresh when tokens are refreshed."""
    callback_calls: list[tuple[str, str]] = []

    def on_refresh(access: str, refresh: str) -> None:
        callback_calls.append((access, refresh))

    svc = GoogleCalendarService(
        access_token="old",
        refresh_token="old-refresh",
        client_id="cid",
        client_secret="csec",
        on_token_refresh=on_refresh,
        token_expires_at=time.time() - 100,
    )

    refresh_response = httpx.Response(
        200,
        json={
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 3600,
        },
        request=httpx.Request("POST", "https://oauth2.googleapis.com/token"),
    )
    api_response = httpx.Response(
        200,
        json={"items": []},
        request=httpx.Request(
            "GET", "https://www.googleapis.com/calendar/v3/calendars/primary/events"
        ),
    )

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post.return_value = refresh_response
        mock_client.request.return_value = api_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        await svc.list_events(
            "primary",
            datetime(2026, 3, 25, tzinfo=UTC),
            datetime(2026, 3, 26, tzinfo=UTC),
        )

    assert len(callback_calls) == 1
    assert callback_calls[0] == ("new-access", "new-refresh")


@pytest.mark.asyncio()
async def test_refresh_failure_propagates() -> None:
    """Should propagate refresh failure."""
    svc = GoogleCalendarService(
        access_token="old",
        refresh_token="bad-refresh",
        client_id="cid",
        client_secret="csec",
        token_expires_at=time.time() - 100,
    )

    refresh_error = httpx.Response(
        400,
        json={"error": "invalid_grant"},
        request=httpx.Request("POST", "https://oauth2.googleapis.com/token"),
    )

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post.return_value = refresh_error
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        with pytest.raises(httpx.HTTPStatusError):
            await svc.list_events(
                "primary",
                datetime(2026, 3, 25, tzinfo=UTC),
                datetime(2026, 3, 26, tzinfo=UTC),
            )


# ---------------------------------------------------------------------------
# Timeout handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_timeout_handling(service: GoogleCalendarService) -> None:
    """Should propagate timeout exceptions."""
    with patch.object(service, "_request", new_callable=AsyncMock) as mock_req:
        mock_req.side_effect = httpx.TimeoutException("Connection timeout")
        with pytest.raises(httpx.TimeoutException):
            await service.list_events(
                "primary",
                datetime(2026, 3, 25, tzinfo=UTC),
                datetime(2026, 3, 26, tzinfo=UTC),
            )
