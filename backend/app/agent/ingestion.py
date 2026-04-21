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
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from backend.app.agent.approval import (
    ApprovalDecision,
    _parse_approval_response,
    classify_approval_response,
    get_approval_gate,
)
from backend.app.agent.concurrency import user_locks
from backend.app.agent.context import get_or_create_conversation
from backend.app.agent.dto import SessionState, StoredMessage
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


def _check_channel_route_enabled(user_id: str, channel: str) -> bool | None:
    """Check whether a channel route is enabled for a user.

    Returns True if the route exists and is enabled, False if it exists
    and is disabled, or None if no route exists (backward compat: treat
    as enabled).
    """
    db = SessionLocal()
    try:
        route = db.query(ChannelRoute).filter_by(user_id=user_id, channel=channel).first()
        if route is None:
            return None
        return route.enabled
    finally:
        db.close()


async def _send_early_typing_indicator(channel: str, chat_id: str) -> None:
    """Fire a typing indicator immediately on message receipt (best-effort).

    Called before pipeline dispatch so the user sees instant feedback that
    the agent received their message. Intentionally omits ``request_id``
    so the typing indicator cannot resolve a webchat SSE response future.
    """
    try:
        from backend.app.bus import OutboundMessage, message_bus

        await message_bus.publish_outbound(
            OutboundMessage(
                channel=channel,
                chat_id=chat_id,
                content="",
                is_typing_indicator=True,
            )
        )
    except Exception:
        logger.debug("Failed to send early typing indicator to %s/%s", channel, chat_id)


async def _send_error_fallback(
    channel: str,
    user: User,
    user_id: str,
    request_id: str = "",
) -> None:
    """Send a fallback error message to the user via the bus.

    When *request_id* is provided the outbound message includes it so the
    web chat SSE response future is resolved and the frontend spinner clears.
    A ``done`` activity event is also published so the activity stream stops
    showing the agent as busy.

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
                request_id=request_id,
            )
        )
        # Clear the frontend "thinking" spinner via the activity stream.
        await message_bus.publish_activity(user_id, {"type": "done", "channel": channel})
    except Exception:
        logger.exception("Failed to send error fallback to user %s", user_id)


def _stamp_route_last_inbound(db: Session, user_id: str, channel: str, sender_id: str) -> None:
    """Mark the resolved ChannelRoute as having received an inbound.

    The channel picker UI flips to "Verified" when this field is non-null,
    giving users end-to-end confirmation that their configured channel
    delivers messages. Called on every inbound resolution so the stamp
    always reflects the most recent successful delivery.
    """
    db.query(ChannelRoute).filter_by(
        user_id=user_id, channel=channel, channel_identifier=sender_id
    ).update({"last_inbound_at": datetime.now(UTC)})


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
                # Skip update if the route is disabled so we don't switch
                # the preferred channel to a disabled one.
                if user.preferred_channel != channel and route.enabled and channel != "webchat":
                    user.preferred_channel = channel
                # Remove stale routes for this user+channel that have a
                # different identifier (e.g. a UUID handle that was replaced
                # by a real phone number).  This ensures the outbound
                # dispatcher always picks up the current address.
                deleted = (
                    db.query(ChannelRoute)
                    .filter_by(user_id=user.id, channel=channel)
                    .filter(ChannelRoute.channel_identifier != sender_id)
                    .delete()
                )
                if deleted:
                    user.channel_identifier = sender_id
                    logger.info(
                        "Cleaned up %d stale %s route(s) for user %s",
                        deleted,
                        channel,
                        user.id,
                    )
                _stamp_route_last_inbound(db, user.id, channel, sender_id)
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
            # Remove stale routes for this user+channel before adding the new one.
            # This prevents the outbound dispatcher from picking up an old
            # identifier (e.g. a UUID handle instead of a real phone number).
            db.query(ChannelRoute).filter_by(user_id=user.id, channel=channel).filter(
                ChannelRoute.channel_identifier != sender_id
            ).delete()
            db.add(ChannelRoute(user_id=user.id, channel=channel, channel_identifier=sender_id))
            user.channel_identifier = sender_id
            user.preferred_channel = channel
            db.flush()
            _stamp_route_last_inbound(db, user.id, channel, sender_id)
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
            db.flush()
            _stamp_route_last_inbound(db, existing.id, channel, sender_id)
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
            _stamp_route_last_inbound(db, user.id, channel, sender_id)
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
# Shared pipeline dispatch
# ---------------------------------------------------------------------------


async def _dispatch_to_pipeline(
    user: User,
    session: SessionState,
    message: StoredMessage,
    media_urls: list[tuple[str, str]],
    channel: str,
    request_id: str = "",
    downloaded_media: list[DownloadedMedia] | None = None,
    download_media: Callable[[str], Awaitable[DownloadedMedia]] | None = None,
) -> None:
    """Acquire the per-user lock, refresh user state, and run the agent pipeline.

    Handles timeout and exception fallbacks. Used by both the direct
    (non-batched) path and the ``MessageBatcher`` flush path.

    While waiting for the per-user lock, a background task polls for a
    stale approval gate left by the previous pipeline. If found, it
    resolves the gate as INTERRUPTED so the previous pipeline can finish
    and release the lock. Without this, a new message that arrives after
    the batcher dispatches the first but before the approval gate is set
    would deadlock: pipeline 1 waits on the gate, pipeline 2 waits on
    the lock, nobody resolves the gate.
    """
    user_id = user.id
    pipeline_error: Exception | None = None
    timed_out = False
    gate = get_approval_gate()

    async def _interrupt_stale_approval() -> None:
        """Resolve a stale approval gate so the previous pipeline releases the lock."""
        try:
            while True:
                await asyncio.sleep(0.5)
                if gate.has_pending(user_id):
                    logger.info(
                        "New message queued for user %s; resolving stale approval as INTERRUPTED",
                        user_id,
                    )
                    gate.resolve(user_id, ApprovalDecision.INTERRUPTED)
                    return
        except asyncio.CancelledError:
            return

    try:
        async with asyncio.timeout(settings.agent_processing_timeout_seconds):
            interrupt_task = asyncio.create_task(_interrupt_stale_approval())
            try:
                async with user_locks.acquire(user_id):
                    interrupt_task.cancel()
                    try:
                        db = SessionLocal()
                        try:
                            fresh = db.query(User).filter_by(id=user_id).first()
                            if fresh is not None:
                                db.expunge(fresh)
                                user = fresh
                        finally:
                            db.close()
                        # Reload session messages from DB so we see any
                        # messages persisted by a previous pipeline that
                        # was holding the lock (e.g. tool interactions
                        # from an interrupted approval).
                        fresh_session = get_session_store(user_id).load_session(session.session_id)
                        if fresh_session is not None:
                            session = fresh_session
                        await handle_inbound_message(
                            user=user,
                            session=session,
                            message=message,
                            media_urls=media_urls,
                            downloaded_media=downloaded_media,
                            channel=channel,
                            request_id=request_id,
                            download_media=download_media,
                        )
                    except Exception as exc:
                        pipeline_error = exc
            finally:
                interrupt_task.cancel()
    except TimeoutError:
        timed_out = True

    # Error fallback runs outside both the timeout and lock scopes so it
    # is never cut short by the same timeout that killed the pipeline.
    if timed_out:
        logger.error(
            "Agent processing timed out after %.0fs for message seq %d (user %s)",
            settings.agent_processing_timeout_seconds,
            message.seq,
            user_id,
        )
        await _send_error_fallback(channel, user, user_id, request_id=request_id)
    elif pipeline_error is not None:
        logger.exception(
            "Agent pipeline failed for message seq %d (user %s)",
            message.seq,
            user_id,
            exc_info=pipeline_error,
        )
        await _send_error_fallback(channel, user, user_id, request_id=request_id)


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

        await _dispatch_to_pipeline(
            user=user,
            session=last_entry.session,
            message=last_entry.message,
            media_urls=all_media,
            channel=state.channel,
            request_id=state.request_id,
            downloaded_media=all_downloaded or None,
            download_media=state.download_media,
        )


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

    # -- Check if channel is disabled for this user --
    route_enabled = _check_channel_route_enabled(user.id, inbound.channel)
    if route_enabled is False:
        from backend.app.bus import OutboundMessage, message_bus

        await message_bus.publish_outbound(
            OutboundMessage(
                channel=inbound.channel,
                chat_id=inbound.sender_id,
                content=(
                    "This channel is currently disabled. Please message from your enabled channel."
                ),
                request_id=inbound.request_id,
            )
        )
        logger.info(
            "Dropped inbound from %s/%s: channel disabled",
            inbound.channel,
            inbound.sender_id,
        )
        return

    # -- Intercept approval responses before normal processing --
    gate = get_approval_gate()
    if gate.has_pending(user.id) and inbound.text:
        # Fast path: exact keyword match
        decision = _parse_approval_response(inbound.text)

        # Slow path: LLM classification for natural-language responses
        # like "Yes to both", "go ahead", "sure thing", etc.
        if decision is None:
            logger.info(
                "Approval pending for user %s but response %r not an exact match, "
                "trying LLM classification",
                user.id,
                inbound.text[:100],
            )
            decision = await classify_approval_response(inbound.text)

        if decision is not None:
            logger.info(
                "Approval resolved for user %s: %s (from %r)",
                user.id,
                decision,
                inbound.text[:100],
            )
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
            # For webchat: resolve the response future so the SSE stream for
            # this approval message closes cleanly with an empty reply.
            if inbound.request_id:
                from backend.app.bus import OutboundMessage, message_bus

                message_bus.resolve_response(
                    inbound.request_id,
                    OutboundMessage(
                        channel=inbound.channel,
                        chat_id=inbound.sender_id,
                        content="",
                    ),
                )
            return

        # The message is unrelated to the pending approval. Interrupt the
        # approval (so the blocked agent loop can finish) and let this
        # message fall through to normal processing.
        logger.info(
            "Approval pending for user %s but message %r is unrelated; resolving as INTERRUPTED",
            user.id,
            inbound.text[:100],
        )
        gate.resolve(user.id, ApprovalDecision.INTERRUPTED)
        # Fall through to normal session/pipeline dispatch below.

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

    # Immediate typing indicator so the user knows the agent saw their message.
    await _send_early_typing_indicator(inbound.channel, inbound.sender_id)

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
        await _dispatch_to_pipeline(
            user=user,
            session=session,
            message=message,
            media_urls=inbound.media_refs,
            channel=inbound.channel,
            request_id=inbound.request_id,
            downloaded_media=inbound.downloaded_media,
            download_media=download_media,
        )
