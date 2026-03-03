"""Channel-agnostic inbound message ingestion.

Defines the ``InboundMessage`` dataclass and ``process_inbound_message()``
which handles the channel-independent steps of receiving a message:
contractor lookup/creation, conversation management, message persistence,
and background task dispatch.
"""

import json
import logging
from dataclasses import dataclass, field

from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from backend.app.agent.concurrency import contractor_locks
from backend.app.agent.context import get_or_create_conversation
from backend.app.agent.router import handle_inbound_message
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


async def _process_message_background(
    contractor_id: int,
    message_id: int,
    media_urls: list[tuple[str, str]],
    messaging_service: MessagingService,
) -> None:
    """Run the agent pipeline as a background task.

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

    task = BackgroundTask(
        _process_message_background,
        contractor_id=contractor.id,
        message_id=message.id,
        media_urls=inbound.media_refs,
        messaging_service=messaging_service,
    )
    return task, contractor, message
