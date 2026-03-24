"""Calendar provider protocol and DTOs.

Defines the CalendarProvider protocol that all calendar integrations must
implement, plus shared Pydantic DTOs for calendar events and availability.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from pydantic import BaseModel

# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


class CalendarEventData(BaseModel):
    """A calendar event returned from a provider."""

    id: str
    title: str
    description: str = ""
    start: datetime
    end: datetime
    location: str = ""
    all_day: bool = False
    status: str = "confirmed"


class CalendarEventCreate(BaseModel):
    """Payload for creating a new calendar event."""

    title: str
    start: datetime
    end: datetime
    description: str = ""
    location: str = ""
    all_day: bool = False


class CalendarEventUpdate(BaseModel):
    """Payload for updating an existing calendar event."""

    title: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    description: str | None = None
    location: str | None = None


class BusySlot(BaseModel):
    """A time range during which the calendar is busy."""

    start: datetime
    end: datetime


class CalendarInfo(BaseModel):
    """Metadata for a calendar."""

    id: str
    summary: str
    primary: bool = False


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class CalendarProvider(Protocol):
    """Interface for calendar integrations."""

    @property
    def provider_name(self) -> str: ...

    async def list_events(
        self,
        calendar_id: str,
        time_min: datetime,
        time_max: datetime,
    ) -> list[CalendarEventData]: ...

    async def create_event(
        self,
        calendar_id: str,
        event: CalendarEventCreate,
    ) -> CalendarEventData: ...

    async def update_event(
        self,
        calendar_id: str,
        event_id: str,
        updates: CalendarEventUpdate,
    ) -> CalendarEventData: ...

    async def delete_event(
        self,
        calendar_id: str,
        event_id: str,
    ) -> None: ...

    async def check_availability(
        self,
        calendar_id: str,
        time_min: datetime,
        time_max: datetime,
    ) -> list[BusySlot]: ...
