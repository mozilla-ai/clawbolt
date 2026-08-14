"""Per-user channel identity linking for multi-user mode.

Supports Telegram (user ID), Linq/iMessage/RCS/SMS (phone number),
BlueBubbles (phone number or iCloud email), and Twilio (phone number,
single shared RCS sender with SMS/MMS fallback via Messaging Service).

Channel identifiers are stored in the OSS ``ChannelRoute`` table. The
shared Twilio sender (RCS messaging service or SMS phone number) lives
in OSS settings; this router only manages the per-user ``From`` lookup
that routes inbound webhooks to the right tenant.

The welcome-text endpoints (``POST /{channel}/welcome``) send a one-shot
onboarding message to the user's linked identifier so they have an
existing thread to reply to. The reply is what kicks off the agent loop:
the inbound webhook resolves to this user via their ``ChannelRoute``,
and the BOOTSTRAP.md memory document drives the rest of onboarding.
"""

import datetime
import logging
import re

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy import update as sa_update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.agent.context import get_or_create_conversation
from backend.app.agent.session_db import get_session_store
from backend.app.auth.dependencies import get_current_user
from backend.app.channel_state import realign_preferred_channel_async
from backend.app.channels import get_channel
from backend.app.config import settings
from backend.app.database import get_async_db
from backend.app.enums import MessageDirection
from backend.app.models import ChannelRoute, User
from backend.app.schemas import (
    BlueBubblesLinkRequest,
    BlueBubblesLinkResponse,
    LinqLinkRequest,
    LinqLinkResponse,
    TelegramLinkRequest,
    TelegramLinkResponse,
    TwilioLinkRequest,
    TwilioLinkResponse,
    WelcomeTextResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/channels", tags=["channels"])

# E.164: '+' followed by 1-15 digits
_E164_RE = re.compile(r"^\+[1-9]\d{1,14}$")


# ---------------------------------------------------------------------------
# Generic channel-link helpers (ChannelRoute is the single source of truth)
# ---------------------------------------------------------------------------


async def _get_channel_link(
    db: AsyncSession, user_id: str, channel: str
) -> tuple[str | None, bool]:
    """Return (identifier, connected) for a channel link."""
    route = (
        await db.execute(
            select(ChannelRoute).where(
                ChannelRoute.user_id == user_id,
                ChannelRoute.channel == channel,
            )
        )
    ).scalar_one_or_none()
    if route is None:
        return None, False
    return route.channel_identifier, True


async def _set_channel_link(
    db: AsyncSession,
    user_id: str,
    channel: str,
    identifier: str,
    conflict_detail: str,
) -> None:
    """Link a channel identifier to a user, raising HTTPException on conflict.

    Single-channel enforcement: after linking, all other non-webchat routes
    are disabled and ``preferred_channel`` is updated so heartbeat routing
    targets the newly linked channel.

    Uniqueness is enforced by the ChannelRoute unique constraint on
    (channel, channel_identifier). We also do an explicit pre-check so we
    can return a friendly 409 message rather than a raw IntegrityError.
    """
    db_user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if db_user is None:
        raise HTTPException(status_code=404, detail="User not found")

    # Check if another user already owns this (channel, identifier) pair.
    existing = (
        await db.execute(
            select(ChannelRoute).where(
                ChannelRoute.channel == channel,
                ChannelRoute.channel_identifier == identifier,
                ChannelRoute.user_id != user_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail=conflict_detail)

    route = (
        await db.execute(
            select(ChannelRoute).where(
                ChannelRoute.user_id == user_id,
                ChannelRoute.channel == channel,
            )
        )
    ).scalar_one_or_none()
    if route:
        route.channel_identifier = identifier
    else:
        db.add(ChannelRoute(user_id=user_id, channel=channel, channel_identifier=identifier))

    # Single-channel enforcement: disable all other non-webchat routes
    await db.execute(
        sa_update(ChannelRoute)
        .where(
            ChannelRoute.user_id == user_id,
            ChannelRoute.channel != channel,
            ChannelRoute.channel != "webchat",
        )
        .values(enabled=False)
    )

    # Update preferred_channel so heartbeat targets the linked channel
    db_user.preferred_channel = channel

    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=conflict_detail) from exc


async def _remove_channel_link(db: AsyncSession, user_id: str, channel: str) -> None:
    """Unlink a channel from a user.

    Repoints ``preferred_channel`` to any other enabled non-webchat route
    when the removed link was the user's preferred. The OSS heartbeat
    lookup is a pure read and does not fix drift, so the write path owns
    the invariant.
    """
    db_user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if db_user is None:
        raise HTTPException(status_code=404, detail="User not found")

    await db.execute(
        delete(ChannelRoute).where(
            ChannelRoute.user_id == user_id,
            ChannelRoute.channel == channel,
        )
    )
    await realign_preferred_channel_async(db, db_user)
    await db.commit()


# ---------------------------------------------------------------------------
# Telegram endpoints
# ---------------------------------------------------------------------------


@router.get("/telegram", response_model=TelegramLinkResponse)
async def get_telegram_link(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> TelegramLinkResponse:
    """Return the current user's linked Telegram user ID."""
    identifier, connected = await _get_channel_link(db, user.id, "telegram")
    return TelegramLinkResponse(telegram_user_id=identifier, connected=connected)


@router.put("/telegram", response_model=TelegramLinkResponse)
async def set_telegram_link(
    body: TelegramLinkRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> TelegramLinkResponse:
    """Link a Telegram user ID to the current user's account.

    Creates or updates the ChannelRoute so inbound Telegram messages
    from this ID are routed to this user. Returns 409 if the Telegram
    user ID is already claimed by another account.
    """
    tid = body.telegram_user_id.strip()
    if not tid:
        raise HTTPException(status_code=422, detail="Telegram user ID cannot be empty")
    if not tid.isdigit():
        raise HTTPException(
            status_code=422,
            detail="Telegram user ID must be a numeric value",
        )

    await _set_channel_link(
        db,
        user.id,
        "telegram",
        tid,
        "This Telegram user ID is already linked to another account",
    )
    return TelegramLinkResponse(telegram_user_id=tid, connected=True)


@router.delete("/telegram", response_model=TelegramLinkResponse)
async def remove_telegram_link(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> TelegramLinkResponse:
    """Unlink the Telegram user ID from the current user's account."""
    await _remove_channel_link(db, user.id, "telegram")
    return TelegramLinkResponse(telegram_user_id=None, connected=False)


# ---------------------------------------------------------------------------
# Linq (iMessage / RCS / SMS) endpoints
# ---------------------------------------------------------------------------


@router.get("/linq", response_model=LinqLinkResponse)
async def get_linq_link(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> LinqLinkResponse:
    """Return the current user's linked phone number for Linq."""
    identifier, connected = await _get_channel_link(db, user.id, "linq")
    return LinqLinkResponse(
        phone_number=identifier,
        connected=connected,
        linq_from_number=settings.linq_from_number,
    )


@router.put("/linq", response_model=LinqLinkResponse)
async def set_linq_link(
    body: LinqLinkRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> LinqLinkResponse:
    """Link a phone number to the current user for Linq (iMessage/RCS/SMS).

    Creates or updates the ChannelRoute so inbound Linq messages from this
    phone number are routed to this user. Returns 409 if the phone number
    is already claimed by another account.
    """
    phone = body.phone_number.strip()
    if not phone:
        raise HTTPException(status_code=422, detail="Phone number cannot be empty")
    if not _E164_RE.match(phone):
        raise HTTPException(
            status_code=422,
            detail="Phone number must be in E.164 format (e.g. +15551234567)",
        )

    await _set_channel_link(
        db,
        user.id,
        "linq",
        phone,
        "This phone number is already linked to another account",
    )
    return LinqLinkResponse(
        phone_number=phone, connected=True, linq_from_number=settings.linq_from_number
    )


@router.delete("/linq", response_model=LinqLinkResponse)
async def remove_linq_link(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> LinqLinkResponse:
    """Unlink the phone number from the current user's account."""
    await _remove_channel_link(db, user.id, "linq")
    return LinqLinkResponse(
        phone_number=None, connected=False, linq_from_number=settings.linq_from_number
    )


# ---------------------------------------------------------------------------
# BlueBubbles (iMessage via self-hosted Mac bridge) endpoints
# ---------------------------------------------------------------------------

# E.164 or simple email pattern for BlueBubbles (supports both phone and iCloud email)
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@router.get("/bluebubbles", response_model=BlueBubblesLinkResponse)
async def get_bluebubbles_link(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> BlueBubblesLinkResponse:
    """Return the current user's linked phone number or email for BlueBubbles."""
    identifier, connected = await _get_channel_link(db, user.id, "bluebubbles")
    return BlueBubblesLinkResponse(phone_number=identifier, connected=connected)


@router.put("/bluebubbles", response_model=BlueBubblesLinkResponse)
async def set_bluebubbles_link(
    body: BlueBubblesLinkRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> BlueBubblesLinkResponse:
    """Link a phone number or email to the current user for BlueBubbles.

    Creates or updates the ChannelRoute so inbound BlueBubbles messages from this
    identifier are routed to this user. Returns 409 if the identifier is already
    claimed by another account.
    """
    identifier = body.phone_number.strip()
    if not identifier:
        raise HTTPException(status_code=422, detail="Phone number or email cannot be empty")
    if not _E164_RE.match(identifier) and not _EMAIL_RE.match(identifier):
        raise HTTPException(
            status_code=422,
            detail="Must be E.164 phone (e.g. +15551234567) or email address",
        )

    await _set_channel_link(
        db,
        user.id,
        "bluebubbles",
        identifier,
        "This identifier is already linked to another account",
    )
    return BlueBubblesLinkResponse(phone_number=identifier, connected=True)


@router.delete("/bluebubbles", response_model=BlueBubblesLinkResponse)
async def remove_bluebubbles_link(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> BlueBubblesLinkResponse:
    """Unlink the BlueBubbles identifier from the current user's account."""
    await _remove_channel_link(db, user.id, "bluebubbles")
    return BlueBubblesLinkResponse(phone_number=None, connected=False)


# ---------------------------------------------------------------------------
# Twilio (RCS via Messaging Service, with SMS/MMS fallback) endpoints
# ---------------------------------------------------------------------------


@router.get("/twilio", response_model=TwilioLinkResponse)
async def get_twilio_link(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> TwilioLinkResponse:
    """Return the current user's linked phone number for Twilio."""
    identifier, connected = await _get_channel_link(db, user.id, "twilio")
    return TwilioLinkResponse(phone_number=identifier, connected=connected)


@router.put("/twilio", response_model=TwilioLinkResponse)
async def set_twilio_link(
    body: TwilioLinkRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> TwilioLinkResponse:
    """Link a phone number to the current user for Twilio.

    Creates or updates the ChannelRoute so inbound Twilio messages from
    this phone number are routed to this user. Returns 409 if the phone
    number is already claimed by another account.
    """
    phone = body.phone_number.strip()
    if not phone:
        raise HTTPException(status_code=422, detail="Phone number cannot be empty")
    if not _E164_RE.match(phone):
        raise HTTPException(
            status_code=422,
            detail="Phone number must be in E.164 format (e.g. +15551234567)",
        )

    await _set_channel_link(
        db,
        user.id,
        "twilio",
        phone,
        "This phone number is already linked to another account",
    )
    return TwilioLinkResponse(phone_number=phone, connected=True)


@router.delete("/twilio", response_model=TwilioLinkResponse)
async def remove_twilio_link(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> TwilioLinkResponse:
    """Unlink the Twilio phone number from the current user's account."""
    await _remove_channel_link(db, user.id, "twilio")
    return TwilioLinkResponse(phone_number=None, connected=False)


# ---------------------------------------------------------------------------
# Welcome-text kickoff
# ---------------------------------------------------------------------------

WELCOME_TEXT = (
    "Hi! This is Clawbolt, your AI assistant for the trades. "
    "Reply to this message and I'll get you set up."
)

WELCOME_COOLDOWN_SECONDS = 60

# In-memory per-(user, channel) rate limit. Sized for the single-instance
# Railway deployment we ship today. Multi-instance scale-out would need to
# move this to Postgres (e.g. a ``welcome_sent_at`` column on ChannelRoute)
# so a user can't click "resend" once per replica.
_last_welcome_at: dict[tuple[str, str], datetime.datetime] = {}


async def _send_welcome_text(
    db: AsyncSession,
    *,
    user_id: str,
    channel: str,
) -> WelcomeTextResponse:
    """Send the onboarding welcome message via *channel* to this user.

    Looks up the user's ``ChannelRoute`` to find the destination identifier,
    enforces a per-user cooldown, calls the channel's ``send_text`` directly
    (synchronous error semantics: the caller sees a 502 if delivery fails),
    and records the outbound message in session history so the agent sees
    the welcome text as prior context when the user replies.
    """
    route = (
        await db.execute(
            select(ChannelRoute).where(
                ChannelRoute.user_id == user_id,
                ChannelRoute.channel == channel,
            )
        )
    ).scalar_one_or_none()
    if route is None:
        raise HTTPException(
            status_code=404,
            detail=f"No {channel} channel linked. Save your number first.",
        )

    key = (user_id, channel)
    now = datetime.datetime.now(datetime.UTC)
    last = _last_welcome_at.get(key)
    if last is not None and (now - last).total_seconds() < WELCOME_COOLDOWN_SECONDS:
        remaining = WELCOME_COOLDOWN_SECONDS - int((now - last).total_seconds())
        raise HTTPException(
            status_code=429,
            detail=f"Welcome text was just sent. Try again in {remaining}s.",
        )

    try:
        sender = get_channel(channel)
    except KeyError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"The {channel} channel is not configured on this server.",
        ) from exc

    # Release the read transaction before the nested session_db writes.
    # In tests the rebound async factory binds every session to one
    # per-test connection with ``join_transaction_mode="create_savepoint"``;
    # leaving this session's autobegun savepoint open would roll the
    # nested writes back when the dep finalizer closes the session
    # without committing.
    await db.commit()

    try:
        await sender.send_text(to=route.channel_identifier, body=WELCOME_TEXT)
    except Exception as exc:
        logger.exception("Welcome text send failed for user %s via %s", user_id, channel)
        raise HTTPException(
            status_code=502,
            detail="Could not deliver the welcome text. You can still text us yourself.",
        ) from exc

    _last_welcome_at[key] = now
    logger.info("Welcome text sent to user %s via %s", user_id, channel)

    # Persist into the user's conversation history so the agent, on the
    # user's reply, sees a real outbound turn ("we sent X, they replied
    # Y") instead of a context-free inbound. Mirrors how heartbeat
    # records its proactive sends (see heartbeat.py).
    #
    # Fail-soft: if the persist raises, the user has already received the
    # text and the cooldown is stamped. Returning 500 here would leave
    # the frontend showing an error toast on a flow the user already
    # succeeded at. The agent will still respond on the reply; it just
    # won't see the welcome as prior context for this one turn.
    try:
        session, _ = await get_or_create_conversation(user_id)
        await get_session_store(user_id).add_message_async(
            session=session,
            direction=MessageDirection.OUTBOUND,
            body=WELCOME_TEXT,
            llm_reply_text=WELCOME_TEXT,
            channel=channel,
        )
    except Exception:
        logger.exception(
            "Welcome text was sent but persistence failed for user %s via %s",
            user_id,
            channel,
        )

    return WelcomeTextResponse(
        sent=True,
        channel=channel,
        channel_identifier=route.channel_identifier,
    )


@router.post("/linq/welcome", response_model=WelcomeTextResponse)
async def send_linq_welcome(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> WelcomeTextResponse:
    """Send the onboarding welcome iMessage/SMS to the linked Linq number."""
    return await _send_welcome_text(db, user_id=user.id, channel="linq")


@router.post("/bluebubbles/welcome", response_model=WelcomeTextResponse)
async def send_bluebubbles_welcome(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> WelcomeTextResponse:
    """Send the onboarding welcome iMessage to the linked BlueBubbles identifier."""
    return await _send_welcome_text(db, user_id=user.id, channel="bluebubbles")


@router.post("/twilio/welcome", response_model=WelcomeTextResponse)
async def send_twilio_welcome(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
) -> WelcomeTextResponse:
    """Send the onboarding welcome text to the linked Twilio number."""
    return await _send_welcome_text(db, user_id=user.id, channel="twilio")
