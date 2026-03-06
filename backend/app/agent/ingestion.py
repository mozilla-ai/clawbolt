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

from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from backend.app.agent.concurrency import contractor_locks
from backend.app.agent.context import get_or_create_conversation
from backend.app.agent.router import handle_inbound_message
from backend.app.config import settings
from backend.app.database import SessionLocal
from backend.app.enums import MessageDirection
from backend.app.models import Contractor, Message
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


def _get_or_create_contractor(db: Session, channel: str, sender_id: str) -> Contractor:
    """Look up or create a contractor by channel-specific sender ID."""
    contractor = db.query(Contractor).filter(Contractor.channel_identifier == sender_id).first()
    if contractor is None:
        contractor = Contractor(
            user_id=f"{channel}_{sender_id}",
            channel_identifier=sender_id,
            preferred_channel=channel,
        )
        db.add(contractor)
        db.commit()
        db.refresh(contractor)
    return contractor


# ---------------------------------------------------------------------------
# Message batcher
# ---------------------------------------------------------------------------


@dataclass
class _BatchEntry:
    """A single message pending in a batch."""

    message_id: int
    media_urls: list[tuple[str, str]]


@dataclass
class _BatchState:
    """Per-contractor batch state: accumulated entries and a flush timer."""

    entries: list[_BatchEntry] = field(default_factory=list)
    timer: asyncio.Task[None] | None = None
    messaging_service: MessagingService | None = None


class MessageBatcher:
    """Groups rapid-fire messages from the same contractor before processing.

    When multiple messages arrive within ``window_ms`` of each other
    (e.g. after a tunnel reconnect or when a user sends several messages
    quickly), they are batched together.  Only the last message triggers the
    agent pipeline; earlier messages are already persisted in the database
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
        contractor_id: int,
        message_id: int,
        media_urls: list[tuple[str, str]],
        messaging_service: MessagingService,
    ) -> None:
        """Add a message to the batch for *contractor_id*.

        Resets the flush timer so that messages arriving within the window
        are grouped together.
        """
        async with self._lock:
            state = self._states.setdefault(contractor_id, _BatchState())
            state.entries.append(_BatchEntry(message_id=message_id, media_urls=media_urls))
            state.messaging_service = messaging_service
            if state.timer is not None:
                state.timer.cancel()
            state.timer = asyncio.create_task(self._flush_after(contractor_id))

    async def _flush_after(self, contractor_id: int) -> None:
        """Wait for the batch window then flush."""
        await asyncio.sleep(self._window_ms / 1000.0)
        await self._flush(contractor_id)

    async def _flush(self, contractor_id: int) -> None:
        """Process the batched messages for *contractor_id*.

        Acquires the per-contractor lock, then runs the agent pipeline for
        the most recent message.  Media from all batched messages is merged.
        """
        async with self._lock:
            state = self._states.pop(contractor_id, None)
        if state is None or not state.entries or state.messaging_service is None:
            return

        last_entry = state.entries[-1]
        messaging_service = state.messaging_service

        # Merge media from all batched messages so attachments are not lost.
        all_media: list[tuple[str, str]] = []
        for entry in state.entries:
            all_media.extend(entry.media_urls)

        if len(state.entries) > 1:
            logger.info(
                "Batched %d messages for contractor %d, processing message %d",
                len(state.entries),
                contractor_id,
                last_entry.message_id,
            )

        async with contractor_locks.acquire(contractor_id):
            db: Session = SessionLocal()
            try:
                contractor = db.get(Contractor, contractor_id)
                message = db.get(Message, last_entry.message_id)
                if contractor is None or message is None:
                    logger.error(
                        "Batch flush: contractor %d or message %d not found",
                        contractor_id,
                        last_entry.message_id,
                    )
                    return
                await handle_inbound_message(
                    db=db,
                    contractor=contractor,
                    message=message,
                    media_urls=all_media,
                    messaging_service=messaging_service,
                )
            except Exception:
                logger.exception(
                    "Agent pipeline failed for message %d (contractor %d)",
                    last_entry.message_id,
                    contractor_id,
                )
            finally:
                db.close()


# Module-level singleton
message_batcher = MessageBatcher(window_ms=settings.message_batch_window_ms)


# ---------------------------------------------------------------------------
# Background task entry points
# ---------------------------------------------------------------------------


async def _process_message_background(
    contractor_id: int,
    message_id: int,
    media_urls: list[tuple[str, str]],
    messaging_service: MessagingService,
) -> None:
    """Run the agent pipeline directly (no batching).

    Used when ``message_batch_window_ms`` is 0.
    Creates its own DB session rather than sharing the request-scoped one,
    which would be closed by the time this task executes.
    """
    async with contractor_locks.acquire(contractor_id):
        db: Session = SessionLocal()
        try:
            contractor = db.get(Contractor, contractor_id)
            message = db.get(Message, message_id)
            if contractor is None or message is None:
                logger.error(
                    "Background task: contractor %d or message %d not found",
                    contractor_id,
                    message_id,
                )
                return
            await handle_inbound_message(
                db=db,
                contractor=contractor,
                message=message,
                media_urls=media_urls,
                messaging_service=messaging_service,
            )
        except Exception:
            logger.exception(
                "Agent pipeline failed for message %d (contractor %d)",
                message_id,
                contractor_id,
            )
        finally:
            db.close()


async def _enqueue_message_background(
    contractor_id: int,
    message_id: int,
    media_urls: list[tuple[str, str]],
    messaging_service: MessagingService,
) -> None:
    """Enqueue a message into the batcher (runs as a Starlette BackgroundTask)."""
    await message_batcher.enqueue(contractor_id, message_id, media_urls, messaging_service)


async def process_inbound_message(
    db: Session,
    inbound: InboundMessage,
    messaging_service: MessagingService,
) -> tuple[BackgroundTask, Contractor, Message]:
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
    contractor = _get_or_create_contractor(db, inbound.channel, inbound.sender_id)
    conversation, _is_new = await get_or_create_conversation(db, contractor.id)

    message = Message(
        conversation_id=conversation.id,
        direction=MessageDirection.INBOUND,
        external_message_id=inbound.external_message_id or None,
        body=inbound.text,
        media_urls_json=json.dumps([file_id for file_id, _mime in inbound.media_refs]),
    )
    db.add(message)
    db.commit()
    db.refresh(message)

    handler = (
        _enqueue_message_background
        if settings.message_batch_window_ms > 0
        else _process_message_background
    )
    task = BackgroundTask(
        handler,
        contractor_id=contractor.id,
        message_id=message.id,
        media_urls=inbound.media_refs,
        messaging_service=messaging_service,
    )
    return task, contractor, message
