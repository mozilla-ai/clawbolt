"""Public waitlist endpoint for capturing interest from unapproved users."""

import re

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.database import get_async_db
from backend.app.middleware.rate_limit import check_rate_limit
from backend.app.models import WAITLIST_NAME_DEFAULT, AllowedEmail, WaitlistEntry
from backend.app.schemas import StatusResponse, WaitlistJoinRequest

router = APIRouter(prefix="/waitlist", tags=["waitlist"])

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Conservative cap. Matches the column width on ``waitlist_entries.name``.
# Anything longer is almost certainly noise (paste of a multi-line bio,
# adversarial payload), so we truncate at the boundary instead of erroring.
_NAME_MAX_LEN = 120
# Use-case field is free-text. Anything beyond a couple of paragraphs is
# almost certainly a paste, so truncate at the boundary.
_USE_CASE_MAX_LEN = 2000


def _normalize_name(raw: str) -> str:
    """Collapse whitespace, drop control chars, cap length, fall back to default."""
    cleaned = " ".join(raw.split())
    cleaned = "".join(ch for ch in cleaned if ch.isprintable())
    cleaned = cleaned[:_NAME_MAX_LEN].strip()
    return cleaned or WAITLIST_NAME_DEFAULT


def _normalize_use_case(raw: str) -> str | None:
    """Strip, drop non-printable chars (keep newlines), cap length, None when empty."""
    cleaned = "".join(ch for ch in raw if ch.isprintable() or ch in "\n\r\t")
    cleaned = cleaned.strip()
    if not cleaned:
        return None
    return cleaned[:_USE_CASE_MAX_LEN]


@router.post("/join", response_model=StatusResponse)
async def join_waitlist(
    body: WaitlistJoinRequest,
    request: Request,
    db: AsyncSession = Depends(get_async_db),
) -> StatusResponse:
    """Add an email to the waitlist.

    Always returns 200 to prevent email enumeration.
    Rate-limited per IP.
    """
    check_rate_limit(request)

    normalized = body.email.lower().strip()
    if not _EMAIL_RE.match(normalized) or len(normalized) > 255:
        raise HTTPException(status_code=422, detail="Invalid email address")

    name = _normalize_name(body.name)
    use_case = _normalize_use_case(body.use_case)
    source = body.source if body.source in ("homepage", "login") else "homepage"

    # Skip if already on the allowed list
    already_allowed = (
        await db.execute(select(AllowedEmail).where(AllowedEmail.email == normalized))
    ).scalar_one_or_none()
    if already_allowed is not None:
        return StatusResponse(status="ok")

    # Skip if already on the waitlist
    existing = (
        await db.execute(select(WaitlistEntry).where(WaitlistEntry.email == normalized))
    ).scalar_one_or_none()
    if existing is not None:
        return StatusResponse(status="ok")

    entry = WaitlistEntry(email=normalized, name=name, use_case=use_case, source=source)
    db.add(entry)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return StatusResponse(status="ok")

    return StatusResponse(status="ok")
