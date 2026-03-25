"""Channel-agnostic inbound message ingestion.

Defines the ``InboundMessage`` dataclass and ``process_inbound_from_bus()``
which handles the channel-independent steps of receiving a message:
user lookup/creation, conversation management, message persistence,
and pipeline dispatch.

Includes ``MessageBatcher`` which groups rapid-fire messages from the same
user (e.g. after a tunnel reconnect) into a single agent pipeline run,
inspired by nanobot's Mochat delay-based batching pattern.
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from backend.app.agent.approval import (
    _parse_approval_response,
    get_approval_gate,
)
from backend.app.agent.concurrency import user_locks
from backend.app.agent.context import get_or_create_conversation
from backend.app.agent.file_store import (
    SessionState,
    StoredMessage,
)
from backend.app.agent.router import handle_inbound_message
from backend.app.agent.session_db import get_session_store
from backend.app.agent.user_db import provision_user
from backend.app.config import settings
from backend.app.database import SessionLocal
from backend.app.enums import MessageDirection
from backend.app.media.download import DownloadedMedia
from backend.app.models import ChannelRoute, User

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InboundMessage:
    """Channel-agnostic representation of an incoming message.

    Produced by channel-specific adapters (Telegram webhook, web chat)
    and consumed by the bus consumer in ``ChannelManager``.
    """

    channel: str
    sender_id: str
    text: str
    media_refs: list[tuple[str, str]] = field(default_factory=list)
    external_message_id: str = ""
    sender_username: str | None = None
    downloaded_media: list[DownloadedMedia] = field(default_factory=list)
    request_id: str = ""
    session_id: str | None = None


async def _send_error_fallback(
    channel: str,
    user: User,
    user_id: str,
) -> None:
    """Send a fallback error message to the user via the bus.

    Swallows any exception so this never propagates.
    """
    to_address = user.channel_identifier or user.phone
    if not to_address or not channel:
        return
    try:
        from backend.app.bus import OutboundMessage, message_bus

        await message_bus.publish_outbound(
            OutboundMessage(
                channel=channel,
                chat_id=to_address,
                content="Sorry, something went wrong processing your message. Please try again.",
            )
        )
    except Exception:
        logger.exception("Failed to send error fallback to user %s", user_id)


async def _get_or_create_user(channel: str, sender_id: str) -> User:
    """Look up or create a user by channel-specific sender ID.

    Handles three distinct scenarios (in order of evaluation):

    1. **Channel route exists** -- the sender has messaged before on this
       channel.  Return the linked user immediately.  This is the common
       path for both OSS and premium.

    2. **Single-tenant reuse (OSS only)** -- exactly one user exists and
       ``settings.premium_plugin`` is not set.  Link the new channel to
       the existing user so that sessions from every channel are visible
       in the dashboard.  Skipped in premium mode to prevent cross-tenant
       linking.

    3. **Sender ID matches an existing user PK (premium only)** -- the
       webchat sends ``sender_id = user.id`` (the UUID primary key).
       Link the existing user to this channel and provision defaults.
       This avoids creating a duplicate account when a premium user
       first opens the webchat.

    If none of the above match, a new ``User`` and ``ChannelRoute`` are
    created.  An ``IntegrityError`` race is handled by re-querying.
    """
    from sqlalchemy.exc import IntegrityError

    logger.debug("_get_or_create_user: channel=%s sender_id=%s", channel, sender_id)
    db = SessionLocal()
    try:
        # Look up by channel route
        route = (
            db.query(ChannelRoute).filter_by(channel=channel, channel_identifier=sender_id).first()
        )
        if route:
            user = db.query(User).filter_by(id=route.user_id).first()
            if user is not None:
                # Track the most recently used channel so heartbeat and other
                # proactive messages are delivered to the right place.
                if user.preferred_channel != channel:
                    user.preferred_channel = channel
                    db.commit()
                    db.refresh(user)
                logger.debug("_get_or_create_user: found via channel route -> user %s", user.id)
                db.expunge(user)
                return user

        # Reuse the sole existing user (single-tenant OSS) so sessions from
        # every channel are visible in the dashboard.  Skip this in
        # multi-tenant (premium) mode to avoid linking a new sender's
        # messages to an existing user's account.
        all_users = db.query(User).all()
        if len(all_users) == 1 and not settings.premium_plugin:
            user = all_users[0]
            logger.debug("_get_or_create_user: single-tenant reuse -> user %s", user.id)
            db.add(ChannelRoute(user_id=user.id, channel=channel, channel_identifier=sender_id))
            user.channel_identifier = sender_id
            user.preferred_channel = channel
            db.commit()
            db.refresh(user)
            db.expunge(user)
            return user

        # In premium mode the webchat sends sender_id = user.id (the PK).
        # Link the existing user to this channel instead of creating a
        # duplicate account.
        existing = db.query(User).filter_by(id=sender_id).first()
        if existing is not None:
            logger.debug(
                "_get_or_create_user: sender_id matches existing PK -> user %s",
                existing.id,
            )
            db.add(ChannelRoute(user_id=existing.id, channel=channel, channel_identifier=sender_id))
            provision_user(existing, db)
            db.commit()
            db.refresh(existing)
            db.expunge(existing)
            return existing

        # Create new user -- handle concurrent creation race
        try:
            user = User(
                user_id=f"{channel}_{sender_id}",
                channel_identifier=sender_id,
                preferred_channel=channel,
            )
            db.add(user)
            db.flush()
            db.add(ChannelRoute(user_id=user.id, channel=channel, channel_identifier=sender_id))
            db.flush()
            provision_user(user, db)
            db.commit()
            db.refresh(user)
            logger.debug(
                "_get_or_create_user: created new user %s (user_id=%s)",
                user.id,
                user.user_id,
            )
            db.expunge(user)
            return user
        except IntegrityError:
            db.rollback()
            logger.debug(
                "_get_or_create_user: IntegrityError race, re-querying for %s/%s",
                channel,
                sender_id,
            )
            # Concurrent insert won the race; re-query
            route = (
                db.query(ChannelRoute)
                .filter_by(channel=channel, channel_identifier=sender_id)
                .first()
            )
            if route:
                user = db.query(User).filter_by(id=route.user_id).first()
                if user is not None:
                    db.expunge(user)
                    return user
            # Fallback: look up by user_id
            user = db.query(User).filter_by(user_id=f"{channel}_{sender_id}").first()
            if user is not None:
                db.expunge(user)
                return user
            raise
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Message batcher
# ---------------------------------------------------------------------------


@dataclass
class _BatchEntry:
    """A single message pending in a batch."""

    session: SessionState
    message: StoredMessage
    media_urls: list[tuple[str, str]]
    downloaded_media: list[DownloadedMedia] = field(default_factory=list)


@dataclass
class _BatchState:
    """Per-user batch state: accumulated entries and a flush timer."""

    entries: list[_BatchEntry] = field(default_factory=list)
    timer: asyncio.Task[None] | None = None
    download_media: Callable[[str], Awaitable[DownloadedMedia]] | None = None
    user: User | None = None
    channel: str = ""
    request_id: str = ""


class MessageBatcher:
    """Groups rapid-fire messages from the same user before processing.

    When multiple messages arrive within ``window_ms`` of each other
    (e.g. after a tunnel reconnect or when a user sends several messages
    quickly), they are batched together.  Only the last message triggers the
    agent pipeline; earlier messages are already persisted in the session
    and appear in conversation history automatically.  Media from all
    batched messages is combined so nothing is lost.

    Modelled after nanobot's Mochat ``_enqueue_delayed_entry`` /
    ``_flush_delayed_entries`` pattern.
    """

    def __init__(self, window_ms: int | None = None) -> None:
        window_ms = window_ms if window_ms is not None else settings.message_batch_window_ms
        self._window_ms = window_ms
        self._states: dict[str, _BatchState] = {}
        self._lock = asyncio.Lock()

    async def enqueue(
        self,
        user: User,
        session: SessionState,
        message: StoredMessage,
        media_urls: list[tuple[str, str]],
        channel: str = "",
        request_id: str = "",
        downloaded_media: list[DownloadedMedia] | None = None,
        download_media: Callable[[str], Awaitable[DownloadedMedia]] | None = None,
    ) -> None:
        """Add a message to the batch for the user.

        Resets the flush timer so that messages arriving within the window
        are grouped together.
        """
        async with self._lock:
            state = self._states.setdefault(user.id, _BatchState())
            state.entries.append(
                _BatchEntry(
                    session=session,
                    message=message,
                    media_urls=media_urls,
                    downloaded_media=downloaded_media or [],
                )
            )
            state.download_media = download_media
            state.user = user
            state.channel = channel
            state.request_id = request_id
            if state.timer is not None:
                state.timer.cancel()
            state.timer = asyncio.create_task(self._flush_after(user.id))

    async def _flush_after(self, user_id: str) -> None:
        """Wait for the batch window then flush."""
        await asyncio.sleep(self._window_ms / 1000.0)
        await self._flush(user_id)

    async def _flush(self, user_id: str) -> None:
        """Process the batched messages for the user.

        Acquires the per-user lock, then runs the agent pipeline for
        the most recent message.  Media from all batched messages is merged.
        """
        async with self._lock:
            state = self._states.pop(user_id, None)
        if state is None or not state.entries:
            return
        if state.user is None:
            return

        last_entry = state.entries[-1]
        user = state.user

        # Merge media from all batched messages so attachments are not lost.
        all_media: list[tuple[str, str]] = []
        all_downloaded: list[DownloadedMedia] = []
        for entry in state.entries:
            all_media.extend(entry.media_urls)
            all_downloaded.extend(entry.downloaded_media)

        if len(state.entries) > 1:
            logger.info(
                "Batched %d messages for user %s, processing message seq %d",
                len(state.entries),
                user_id,
                last_entry.message.seq,
            )

        async with user_locks.acquire(user_id):
            try:
                # Reload user in case it was updated
                db = SessionLocal()
                try:
                    fresh = db.query(User).filter_by(id=user_id).first()
                    if fresh is not None:
                        db.expunge(fresh)
                        user = fresh
                finally:
                    db.close()
                await handle_inbound_message(
                    user=user,
                    session=last_entry.session,
                    message=last_entry.message,
                    media_urls=all_media,
                    downloaded_media=all_downloaded or None,
                    channel=state.channel,
                    request_id=state.request_id,
                    download_media=state.download_media,
                )
            except Exception:
                logger.exception(
                    "Agent pipeline failed for message seq %d (user %s)",
                    last_entry.message.seq,
                    user_id,
                )
                await _send_error_fallback(state.channel, user, user_id)


# Module-level singleton
message_batcher = MessageBatcher(window_ms=settings.message_batch_window_ms)


# ---------------------------------------------------------------------------
# Bus consumer entry point
# ---------------------------------------------------------------------------


async def process_inbound_from_bus(
    inbound: "InboundMessage",
    download_media: Callable[[str], Awaitable[DownloadedMedia]] | None = None,
) -> None:
    """Process an inbound message consumed from the bus.

    Handles user lookup/creation, session management, message
    persistence, and dispatches to the agent pipeline (with optional
    batching).
    """
    user = await _get_or_create_user(inbound.channel, inbound.sender_id)
    logger.debug(
        "process_inbound_from_bus: resolved user %s for channel=%s sender_id=%s",
        user.id,
        inbound.channel,
        inbound.sender_id,
    )

    # -- Intercept approval responses before normal processing --
    gate = get_approval_gate()
    if gate.has_pending(user.id) and inbound.text:
        decision = _parse_approval_response(inbound.text)
        if decision is not None:
            gate.resolve(user.id, decision)
            # Persist the reply to the session so it appears in conversation history
            session, _is_new = await get_or_create_conversation(
                user.id, external_session_id=inbound.session_id
            )
            session_store = get_session_store(user.id)
            await session_store.add_message(
                session=session,
                direction=MessageDirection.INBOUND,
                body=inbound.text,
                external_message_id=inbound.external_message_id or "",
                media_urls_json=json.dumps([file_id for file_id, _mime in inbound.media_refs]),
                channel=inbound.channel,
            )
            return

    session, _is_new = await get_or_create_conversation(
        user.id, external_session_id=inbound.session_id
    )
    logger.debug(
        "process_inbound_from_bus: session %s (new=%s) for user %s",
        session.session_id,
        _is_new,
        user.id,
    )

    session_store = get_session_store(user.id)
    message = await session_store.add_message(
        session=session,
        direction=MessageDirection.INBOUND,
        body=inbound.text,
        external_message_id=inbound.external_message_id or "",
        media_urls_json=json.dumps([file_id for file_id, _mime in inbound.media_refs]),
        channel=inbound.channel,
    )

    if settings.message_batch_window_ms > 0:
        await message_batcher.enqueue(
            user=user,
            session=session,
            message=message,
            media_urls=inbound.media_refs,
            channel=inbound.channel,
            request_id=inbound.request_id,
            downloaded_media=inbound.downloaded_media or None,
            download_media=download_media,
        )
    else:
        async with user_locks.acquire(user.id):
            try:
                db = SessionLocal()
                try:
                    fresh = db.query(User).filter_by(id=user.id).first()
                    if fresh is not None:
                        db.expunge(fresh)
                        user = fresh
                finally:
                    db.close()
                await handle_inbound_message(
                    user=user,
                    session=session,
                    message=message,
                    media_urls=inbound.media_refs,
                    downloaded_media=inbound.downloaded_media,
                    channel=inbound.channel,
                    request_id=inbound.request_id,
                    download_media=download_media,
                )
            except Exception:
                logger.exception(
                    "Agent pipeline failed for message seq %d (user %s)",
                    message.seq,
                    user.id,
                )
                await _send_error_fallback(inbound.channel, user, user.id)
