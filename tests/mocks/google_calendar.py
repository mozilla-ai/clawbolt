"""Mock Google Calendar service for testing."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from backend.app.services.calendar_provider import (
    BusySlot,
    CalendarEventCreate,
    CalendarEventData,
    CalendarEventUpdate,
    CalendarInfo,
)

# Sample events for the mock, keyed by calendar_id.
_SAMPLE_EVENTS: list[dict[str, Any]] = [
    {
        "id": "evt-001",
        "title": "Job: Smith Kitchen Remodel",
        "description": "Full kitchen renovation. Demo cabinets, install new counters.",
        "start": datetime(2026, 3, 25, 9, 0, tzinfo=UTC),
        "end": datetime(2026, 3, 25, 17, 0, tzinfo=UTC),
        "location": "123 Oak St, Portland OR",
        "all_day": False,
        "status": "confirmed",
        "calendar_id": "primary",
    },
    {
        "id": "evt-002",
        "title": "Job: Jones Roof Repair",
        "description": "Patch leak in east-facing section.",
        "start": datetime(2026, 3, 26, 8, 0, tzinfo=UTC),
        "end": datetime(2026, 3, 26, 12, 0, tzinfo=UTC),
        "location": "456 Elm Ave, Seattle WA",
        "all_day": False,
        "status": "confirmed",
        "calendar_id": "jobs@example.com",
    },
]

_NEXT_ID = 100


class MockGoogleCalendarService:
    """In-memory Google Calendar service for testing.

    Implements the same interface as GoogleCalendarService without
    making any HTTP calls.
    """

    def __init__(self) -> None:
        global _NEXT_ID
        _NEXT_ID = 100
        self.events: list[CalendarEventData] = [
            CalendarEventData(**{k: v for k, v in e.items() if k != "calendar_id"})
            for e in _SAMPLE_EVENTS
        ]
        self._event_calendar_map: dict[str, str] = {
            e["id"]: e["calendar_id"] for e in _SAMPLE_EVENTS
        }
        self.busy_slots: list[BusySlot] = [
            BusySlot(
                start=datetime(2026, 3, 25, 9, 0, tzinfo=UTC),
                end=datetime(2026, 3, 25, 17, 0, tzinfo=UTC),
            ),
        ]
        self.calendars: list[CalendarInfo] = [
            CalendarInfo(id="primary", summary="Personal", primary=True),
            CalendarInfo(id="jobs@example.com", summary="Jobs", primary=False),
        ]

    @property
    def provider_name(self) -> str:
        return "google_calendar"

    async def list_calendars(self) -> list[CalendarInfo]:
        return list(self.calendars)

    async def list_events(
        self,
        calendar_id: str,
        time_min: datetime,
        time_max: datetime,
    ) -> list[CalendarEventData]:
        return [
            e
            for e in self.events
            if e.start >= time_min
            and e.start < time_max
            and self._event_calendar_map.get(e.id, "primary") == calendar_id
        ]

    async def create_event(
        self,
        calendar_id: str,
        event: CalendarEventCreate,
    ) -> CalendarEventData:
        global _NEXT_ID
        new_event = CalendarEventData(
            id=f"evt-{_NEXT_ID}",
            title=event.title,
            description=event.description,
            start=event.start,
            end=event.end,
            location=event.location,
            all_day=event.all_day,
            status="confirmed",
        )
        _NEXT_ID += 1
        self.events.append(new_event)
        self._event_calendar_map[new_event.id] = calendar_id
        return new_event

    async def update_event(
        self,
        calendar_id: str,
        event_id: str,
        updates: CalendarEventUpdate,
    ) -> CalendarEventData:
        for i, e in enumerate(self.events):
            if e.id == event_id:
                data = e.model_dump()
                if updates.title is not None:
                    data["title"] = updates.title
                if updates.description is not None:
                    data["description"] = updates.description
                if updates.location is not None:
                    data["location"] = updates.location
                if updates.start is not None:
                    data["start"] = updates.start
                if updates.end is not None:
                    data["end"] = updates.end
                updated = CalendarEventData(**data)
                self.events[i] = updated
                return updated
        msg = f"Event {event_id} not found"
        raise ValueError(msg)

    async def delete_event(
        self,
        calendar_id: str,
        event_id: str,
    ) -> None:
        for i, e in enumerate(self.events):
            if e.id == event_id:
                self.events.pop(i)
                self._event_calendar_map.pop(event_id, None)
                return
        msg = f"Event {event_id} not found"
        raise ValueError(msg)

    async def check_availability(
        self,
        calendar_id: str,
        time_min: datetime,
        time_max: datetime,
    ) -> list[BusySlot]:
        return [s for s in self.busy_slots if s.start >= time_min and s.start < time_max]
