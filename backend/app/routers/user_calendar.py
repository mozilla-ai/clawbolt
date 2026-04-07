"""Calendar configuration endpoints.

Lets the user list their Google calendars and choose which ones the
AI agent is allowed to access, with per-calendar tool permissions.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException

from backend.app.agent.tools.calendar_tools import parse_disabled_tools
from backend.app.auth.dependencies import get_current_user
from backend.app.config import settings
from backend.app.database import SessionLocal
from backend.app.models import CalendarConfig, User
from backend.app.schemas import (
    CalendarConfigEntry,
    CalendarConfigResponse,
    CalendarConfigUpdate,
    CalendarListEntry,
    CalendarListResponse,
)
from backend.app.services.google_calendar import GoogleCalendarService
from backend.app.services.oauth import oauth_service

logger = logging.getLogger(__name__)

router = APIRouter()


async def _get_calendar_service(user: User) -> GoogleCalendarService:
    """Build a GoogleCalendarService for the current user or raise 400."""
    if not settings.google_calendar_client_id or not settings.google_calendar_client_secret:
        raise HTTPException(status_code=400, detail="Google Calendar not configured")

    token = await oauth_service.get_valid_token(user.id, "google_calendar")
    if token is None or not token.access_token:
        raise HTTPException(status_code=400, detail="Google Calendar not connected")

    return GoogleCalendarService(
        access_token=token.access_token,
        refresh_token=token.refresh_token,
        client_id=settings.google_calendar_client_id,
        client_secret=settings.google_calendar_client_secret,
        token_expires_at=token.expires_at,
    )


@router.get("/user/calendar/calendars", response_model=CalendarListResponse)
async def list_calendars(
    current_user: User = Depends(get_current_user),
) -> CalendarListResponse:
    """Fetch the user's Google Calendar list from the Google API."""
    service = await _get_calendar_service(current_user)
    try:
        calendars = await service.list_calendars()
    except Exception as exc:
        logger.exception("Failed to list calendars for user %s", current_user.id)
        raise HTTPException(status_code=502, detail=f"Google Calendar error: {exc}") from exc

    return CalendarListResponse(
        calendars=[
            CalendarListEntry(
                id=c.id, summary=c.summary, primary=c.primary, access_role=c.access_role
            )
            for c in calendars
        ]
    )


@router.get("/user/calendar/config", response_model=CalendarConfigResponse)
async def get_calendar_config(
    current_user: User = Depends(get_current_user),
) -> CalendarConfigResponse:
    """Get all enabled calendars for the user."""
    db = SessionLocal()
    try:
        configs = (
            db.query(CalendarConfig)
            .filter_by(user_id=current_user.id, provider="google_calendar")
            .all()
        )
    finally:
        db.close()

    return CalendarConfigResponse(
        calendars=[
            CalendarConfigEntry(
                calendar_id=c.calendar_id,
                display_name=c.display_name,
                disabled_tools=parse_disabled_tools(c.disabled_tools),
                access_role=c.access_role or "",
            )
            for c in configs
        ]
    )


@router.put("/user/calendar/config", response_model=CalendarConfigResponse)
async def update_calendar_config(
    body: CalendarConfigUpdate,
    current_user: User = Depends(get_current_user),
) -> CalendarConfigResponse:
    """Replace all enabled calendars (delete existing, insert new)."""
    db = SessionLocal()
    try:
        db.query(CalendarConfig).filter_by(
            user_id=current_user.id, provider="google_calendar"
        ).delete()

        new_configs: list[CalendarConfig] = []
        for entry in body.calendars:
            config = CalendarConfig(
                user_id=current_user.id,
                provider="google_calendar",
                calendar_id=entry.calendar_id,
                display_name=entry.display_name,
                disabled_tools=json.dumps(entry.disabled_tools) if entry.disabled_tools else "",
                access_role=entry.access_role,
            )
            db.add(config)
            new_configs.append(config)

        db.commit()
        for c in new_configs:
            db.refresh(c)

        return CalendarConfigResponse(
            calendars=[
                CalendarConfigEntry(
                    calendar_id=c.calendar_id,
                    display_name=c.display_name,
                    disabled_tools=parse_disabled_tools(c.disabled_tools),
                    access_role=c.access_role or "",
                )
                for c in new_configs
            ]
        )
    finally:
        db.close()
