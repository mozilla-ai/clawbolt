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
    UserData,
    get_session_store,
    get_user_store,
)
from backend.app.agent.router import handle_inbound_message
from backend.app.config import settings
from backend.app.enums import MessageDirection
from backend.app.media.download import DownloadedMedia
from backend.app.services.messaging import MessagingService

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


async def _get_or_create_user(channel: str, sender_id: str) -> UserData:
    """Look up or create a user by channel-specific sender ID.

    In single-tenant (OSS) mode there should be exactly one user shared
    across all channels.  When a new channel arrives and a user already
    exists, link the channel to that user instead of creating a duplicate.
    """
    store = get_user_store()
    user = await store.get_by_channel(sender_id)
    if user is not None:
        return user

    # Reuse the sole existing user (single-tenant OSS) so sessions from
    # every channel are visible in the dashboard.
    all_users = await store.list_all()
    if len(all_users) == 1:
        user = all_users[0]
        store.link_channel(channel, sender_id, user.id)
        user = await store.update(
            user.id,
            channel_identifier=sender_id,
            preferred_channel=channel,
        )
        return user  # type: ignore[return-value]

    user = await store.create(
        user_id=f"{channel}_{sender_id}",
        channel_identifier=sender_id,
        preferred_channel=channel,
    )
    return user


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
    messaging_service: MessagingService | None = None
    user: UserData | None = None
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

    def __init__(self, window_ms: int = 1500) -> None:
        self._window_ms = window_ms
        self._states: dict[int, _BatchState] = {}
        self._lock = asyncio.Lock()

    async def enqueue(
        self,
        user: UserData,
        session: SessionState,
        message: StoredMessage,
        media_urls: list[tuple[str, str]],
        messaging_service: MessagingService,
        channel: str = "",
        request_id: str = "",
        downloaded_media: list[DownloadedMedia] | None = None,
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
            state.messaging_service = messaging_service
            state.user = user
            state.channel = channel
            state.request_id = request_id
            if state.timer is not None:
                state.timer.cancel()
            state.timer = asyncio.create_task(self._flush_after(user.id))

    async def _flush_after(self, user_id: int) -> None:
        """Wait for the batch window then flush."""
        await asyncio.sleep(self._window_ms / 1000.0)
        await self._flush(user_id)

    async def _flush(self, user_id: int) -> None:
        """Process the batched messages for the user.

        Acquires the per-user lock, then runs the agent pipeline for
        the most recent message.  Media from all batched messages is merged.
        """
        async with self._lock:
            state = self._states.pop(user_id, None)
        if state is None or not state.entries or state.messaging_service is None:
            return
        if state.user is None:
            return

        last_entry = state.entries[-1]
        messaging_service = state.messaging_service
        user = state.user

        # Merge media from all batched messages so attachments are not lost.
        all_media: list[tuple[str, str]] = []
        all_downloaded: list[DownloadedMedia] = []
        for entry in state.entries:
            all_media.extend(entry.media_urls)
            all_downloaded.extend(entry.downloaded_media)

        if len(state.entries) > 1:
            logger.info(
                "Batched %d messages for user %d, processing message seq %d",
                len(state.entries),
                user_id,
                last_entry.message.seq,
            )

        async with user_locks.acquire(user_id):
            try:
                # Reload user in case it was updated
                store = get_user_store()
                fresh = await store.get_by_id(user_id)
                if fresh is not None:
                    user = fresh
                await handle_inbound_message(
                    user=user,
                    session=last_entry.session,
                    message=last_entry.message,
                    media_urls=all_media,
                    messaging_service=messaging_service,
                    downloaded_media=all_downloaded or None,
                    channel=state.channel,
                    request_id=state.request_id,
                )
            except Exception:
                logger.exception(
                    "Agent pipeline failed for message seq %d (user %d)",
                    last_entry.message.seq,
                    user_id,
                )


# Module-level singleton
message_batcher = MessageBatcher(window_ms=settings.message_batch_window_ms)


# ---------------------------------------------------------------------------
# Bus consumer entry point
# ---------------------------------------------------------------------------


async def process_inbound_from_bus(
    inbound: "InboundMessage",
    messaging_service: MessagingService,
) -> None:
    """Process an inbound message consumed from the bus.

    Handles user lookup/creation, session management, message
    persistence, and dispatches to the agent pipeline (with optional
    batching).
    """
    user = await _get_or_create_user(inbound.channel, inbound.sender_id)

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
            messaging_service=messaging_service,
            channel=inbound.channel,
            request_id=inbound.request_id,
            downloaded_media=inbound.downloaded_media or None,
        )
    else:
        async with user_locks.acquire(user.id):
            try:
                store = get_user_store()
                fresh = await store.get_by_id(user.id)
                if fresh is not None:
                    user = fresh
                await handle_inbound_message(
                    user=user,
                    session=session,
                    message=message,
                    media_urls=inbound.media_refs,
                    messaging_service=messaging_service,
                    downloaded_media=inbound.downloaded_media,
                    channel=inbound.channel,
                    request_id=inbound.request_id,
                )
            except Exception:
                logger.exception(
                    "Agent pipeline failed for message seq %d (user %d)",
                    message.seq,
                    user.id,
                )
