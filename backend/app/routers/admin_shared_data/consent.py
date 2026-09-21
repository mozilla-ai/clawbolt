"""Date-range parsing and the consent gate every route here passes through."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import (
    User,
)

router = APIRouter()


def _parse_date_range(
    start_date: str | None, end_date: str | None
) -> tuple[datetime | None, datetime | None] | None:
    """Parse optional ISO-8601 date / datetime strings to a ``(start, end)`` tuple.

    Returns ``None`` when both inputs are absent or empty (no filter
    applies). Raises ``HTTPException(400)`` on malformed inputs so the
    admin gets a clear failure rather than silent permissive behavior.

    Accepts both date-only (``2026-05-01``) and full ISO-8601
    timestamps. A bare date is interpreted as the start (00:00:00 UTC)
    or end (23:59:59 UTC) of that day depending on which slot it
    occupies, so the human-friendly ``start=2026-05-01 end=2026-05-31``
    behaves intuitively as a closed-closed range.
    """
    from datetime import time

    if not start_date and not end_date:
        return None

    def _parse(value: str, *, end_of_day: bool) -> datetime:
        try:
            # ``fromisoformat`` accepts both ``YYYY-MM-DD`` and full
            # ISO-8601 timestamps in modern Python; try the date-only
            # path first so we can lift to start/end-of-day.
            if len(value) == 10 and value.count("-") == 2:
                d = datetime.fromisoformat(value).date()
                return datetime.combine(d, time(23, 59, 59) if end_of_day else time.min, tzinfo=UTC)
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid date '{value}': {exc}",
            ) from exc

    start_dt = _parse(start_date, end_of_day=False) if start_date else None
    end_dt = _parse(end_date, end_of_day=True) if end_date else None
    if start_dt is not None and end_dt is not None and start_dt > end_dt:
        raise HTTPException(
            status_code=400,
            detail=f"start_date {start_date} is after end_date {end_date}",
        )
    return (start_dt, end_dt)


async def _require_consenting_user(db: AsyncSession, user_id: str) -> User:
    """Resolve *user_id* to a User row that has data sharing consent.

    Returns the User on success; raises ``HTTPException(403)`` when the
    user exists but has not consented, or ``HTTPException(404)`` when
    the user doesn't exist. The 403/404 split matters for forensic
    purposes: a 403 leaves an audit row showing "admin tried to read
    a non-consenting user's data" which is itself useful signal.
    """
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if not user.data_sharing_consent:
        raise HTTPException(
            status_code=403,
            detail="User has not consented to data sharing.",
        )
    return user
