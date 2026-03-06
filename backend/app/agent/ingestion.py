"""Channel-agnostic inbound message ingestion.

Defines the ``InboundMessage`` dataclass and ``process_inbound_message()``
which handles the channel-independent steps of receiving a message:
contractor lookup/creation, conversation management, message persistence,
and background task dispatch.

Includes ``MessageBatcher`` which groups rapid-fire messages from the same
contractor (e.g. after a tunnel reconnect) into a single agent pipeline run,
inspired by nanobot's Mochat delay-based batching pattern.
"""

import asyncio
import json
import logging
from dataclasses import dataclass, field

from starlette.background import BackgroundTask

from backend.app.agent.concurrency import contractor_locks
from backend.app.agent.context import get_or_create_conversation
from backend.app.agent.file_store import (
    ContractorData,
    SessionState,
    StoredMessage,
    get_contractor_store,
    get_session_store,
)
from backend.app.agent.router import handle_inbound_message
from backend.app.config import settings
from backend.app.enums import MessageDirection
from backend.app.services.messaging import MessagingService

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class InboundMessage:
    """Channel-agnostic representation of an incoming message.

    Produced by channel-specific adapters (Telegram webhook, future SMS/web)
    and consumed by ``process_inbound_message()``.
    """

    channel: str
    sender_id: str
    text: str
    media_refs: list[tuple[str, str]] = field(default_factory=list)
    external_message_id: str = ""
    sender_username: str | None = None


async def _get_or_create_contractor(channel: str, sender_id: str) -> ContractorData:
    """Look up or create a contractor by channel-specific sender ID."""
    store = get_contractor_store()
    contractor = await store.get_by_channel(sender_id)
    if contractor is None:
        contractor = await store.create(
            user_id=f"{channel}_{sender_id}",
            channel_identifier=sender_id,
            preferred_channel=channel,
        )
    return contractor


# ---------------------------------------------------------------------------
# Message batcher
# ---------------------------------------------------------------------------


@dataclass
class _BatchEntry:
    """A single message pending in a batch."""

    session: SessionState
    message: StoredMessage
    media_urls: list[tuple[str, str]]


@dataclass
class _BatchState:
    """Per-contractor batch state: accumulated entries and a flush timer."""

    entries: list[_BatchEntry] = field(default_factory=list)
    timer: asyncio.Task[None] | None = None
    messaging_service: MessagingService | None = None
    contractor: ContractorData | None = None


class MessageBatcher:
    """Groups rapid-fire messages from the same contractor before processing.

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
        contractor: ContractorData,
        session: SessionState,
        message: StoredMessage,
        media_urls: list[tuple[str, str]],
        messaging_service: MessagingService,
    ) -> None:
        """Add a message to the batch for the contractor.

        Resets the flush timer so that messages arriving within the window
        are grouped together.
        """
        async with self._lock:
            state = self._states.setdefault(contractor.id, _BatchState())
            state.entries.append(
                _BatchEntry(session=session, message=message, media_urls=media_urls)
            )
            state.messaging_service = messaging_service
            state.contractor = contractor
            if state.timer is not None:
                state.timer.cancel()
            state.timer = asyncio.create_task(self._flush_after(contractor.id))

    async def _flush_after(self, contractor_id: int) -> None:
        """Wait for the batch window then flush."""
        await asyncio.sleep(self._window_ms / 1000.0)
        await self._flush(contractor_id)

    async def _flush(self, contractor_id: int) -> None:
        """Process the batched messages for the contractor.

        Acquires the per-contractor lock, then runs the agent pipeline for
        the most recent message.  Media from all batched messages is merged.
        """
        async with self._lock:
            state = self._states.pop(contractor_id, None)
        if state is None or not state.entries or state.messaging_service is None:
            return
        if state.contractor is None:
            return

        last_entry = state.entries[-1]
        messaging_service = state.messaging_service
        contractor = state.contractor

        # Merge media from all batched messages so attachments are not lost.
        all_media: list[tuple[str, str]] = []
        for entry in state.entries:
            all_media.extend(entry.media_urls)

        if len(state.entries) > 1:
            logger.info(
                "Batched %d messages for contractor %d, processing message seq %d",
                len(state.entries),
                contractor_id,
                last_entry.message.seq,
            )

        async with contractor_locks.acquire(contractor_id):
            try:
                # Reload contractor in case it was updated
                store = get_contractor_store()
                fresh = await store.get_by_id(contractor_id)
                if fresh is not None:
                    contractor = fresh
                await handle_inbound_message(
                    contractor=contractor,
                    session=last_entry.session,
                    message=last_entry.message,
                    media_urls=all_media,
                    messaging_service=messaging_service,
                )
            except Exception:
                logger.exception(
                    "Agent pipeline failed for message seq %d (contractor %d)",
                    last_entry.message.seq,
                    contractor_id,
                )


# Module-level singleton
message_batcher = MessageBatcher(window_ms=settings.message_batch_window_ms)


# ---------------------------------------------------------------------------
# Background task entry points
# ---------------------------------------------------------------------------


async def _process_message_background(
    contractor: ContractorData,
    session: SessionState,
    message: StoredMessage,
    media_urls: list[tuple[str, str]],
    messaging_service: MessagingService,
) -> None:
    """Run the agent pipeline directly (no batching).

    Used when ``message_batch_window_ms`` is 0.
    """
    async with contractor_locks.acquire(contractor.id):
        try:
            # Reload contractor
            store = get_contractor_store()
            fresh = await store.get_by_id(contractor.id)
            if fresh is not None:
                contractor = fresh
            await handle_inbound_message(
                contractor=contractor,
                session=session,
                message=message,
                media_urls=media_urls,
                messaging_service=messaging_service,
            )
        except Exception:
            logger.exception(
                "Agent pipeline failed for message seq %d (contractor %d)",
                message.seq,
                contractor.id,
            )


async def _enqueue_message_background(
    contractor: ContractorData,
    session: SessionState,
    message: StoredMessage,
    media_urls: list[tuple[str, str]],
    messaging_service: MessagingService,
) -> None:
    """Enqueue a message into the batcher (runs as a Starlette BackgroundTask)."""
    await message_batcher.enqueue(contractor, session, message, media_urls, messaging_service)


async def process_inbound_message(
    inbound: InboundMessage,
    messaging_service: MessagingService,
) -> tuple[BackgroundTask, ContractorData, StoredMessage]:
    """Channel-agnostic inbound message processing.

    1. Look up or create the contractor from ``inbound.sender_id``
    2. Get or create an active conversation
    3. Persist the inbound message record
    4. Return a background task that runs the agent pipeline

    When ``message_batch_window_ms > 0``, rapid-fire messages from the same
    contractor are batched: only the last message triggers the pipeline while
    earlier messages appear in conversation history.  When the window is 0,
    messages are processed directly without batching.

    Returns (background_task, contractor, message) so the caller can
    include the task in its HTTP response.
    """
    contractor = await _get_or_create_contractor(inbound.channel, inbound.sender_id)
    session, _is_new = await get_or_create_conversation(contractor.id)

    session_store = get_session_store(contractor.id)
    message = await session_store.add_message(
        session=session,
        direction=MessageDirection.INBOUND,
        body=inbound.text,
        external_message_id=inbound.external_message_id or "",
        media_urls_json=json.dumps([file_id for file_id, _mime in inbound.media_refs]),
    )

    handler = (
        _enqueue_message_background
        if settings.message_batch_window_ms > 0
        else _process_message_background
    )
    task = BackgroundTask(
        handler,
        contractor=contractor,
        session=session,
        message=message,
        media_urls=inbound.media_refs,
        messaging_service=messaging_service,
    )
    return task, contractor, message
