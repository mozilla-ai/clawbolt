"""Telegram webhook endpoint for inbound messages."""

import json
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from backend.app.agent.router import handle_inbound_message
from backend.app.config import settings
from backend.app.database import get_db
from backend.app.models import Contractor, Conversation, Message
from backend.app.services.messaging import MessagingService, get_messaging_service

logger = logging.getLogger(__name__)

router = APIRouter()


def _validate_webhook_secret(request: Request) -> None:
    """Validate the Telegram webhook secret token header."""
    if not settings.telegram_webhook_secret:
        return
    header = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if header != settings.telegram_webhook_secret:
        logger.warning("Invalid Telegram webhook secret")
        # Still return 200 to avoid Telegram retries, but log the warning.
        # The message will not be processed (early return in the endpoint).
        raise _InvalidSecret


class _InvalidSecret(Exception):
    pass


def _get_or_create_contractor(db: Session, chat_id: str) -> Contractor:
    """Look up or create a contractor by Telegram chat_id."""
    contractor = db.query(Contractor).filter(Contractor.channel_identifier == chat_id).first()
    if contractor is None:
        contractor = Contractor(
            user_id=f"tg_{chat_id}",
            channel_identifier=chat_id,
            preferred_channel="telegram",
        )
        db.add(contractor)
        db.commit()
        db.refresh(contractor)
    return contractor


def _get_or_create_conversation(db: Session, contractor: Contractor) -> Conversation:
    """Get the active conversation or create a new one."""
    conversation = (
        db.query(Conversation)
        .filter(Conversation.contractor_id == contractor.id, Conversation.is_active.is_(True))
        .first()
    )
    if conversation is None:
        conversation = Conversation(contractor_id=contractor.id)
        db.add(conversation)
        db.commit()
        db.refresh(conversation)
    return conversation


async def _process_message_background(
    db: Session,
    contractor: Contractor,
    message: Message,
    media_urls: list[tuple[str, str]],
    messaging_service: MessagingService,
) -> None:
    """Run the agent pipeline as a background task."""
    try:
        await handle_inbound_message(
            db=db,
            contractor=contractor,
            message=message,
            media_urls=media_urls,
            messaging_service=messaging_service,
        )
    except Exception:
        logger.exception(
            "Agent pipeline failed for message %d from chat %s",
            message.id,
            contractor.channel_identifier,
        )


def _extract_telegram_media(
    update: dict,
) -> list[tuple[str, str]]:
    """Extract media file_ids from a Telegram update.

    Returns a list of (file_id, mime_type) tuples.
    """
    msg = update.get("message", {})
    media: list[tuple[str, str]] = []

    # Photo: Telegram sends multiple sizes — pick the largest
    photos = msg.get("photo")
    if photos:
        largest = max(photos, key=lambda p: p.get("file_size", 0))
        media.append((largest["file_id"], "image/jpeg"))

    # Voice note
    voice = msg.get("voice")
    if voice:
        media.append((voice["file_id"], voice.get("mime_type", "audio/ogg")))

    # Document
    doc = msg.get("document")
    if doc:
        media.append((doc["file_id"], doc.get("mime_type", "application/octet-stream")))

    return media


@router.post("/webhooks/telegram")
async def telegram_inbound(
    request: Request,
    db: Session = Depends(get_db),
    messaging_service: MessagingService = Depends(get_messaging_service),
) -> JSONResponse:
    """Receive inbound messages from Telegram."""
    try:
        _validate_webhook_secret(request)
    except _InvalidSecret:
        return JSONResponse(content={"ok": True})

    update: dict = await request.json()  # type: ignore[assignment]
    msg = update.get("message")
    if not msg:
        # Not a message update (could be edited_message, callback_query, etc.)
        return JSONResponse(content={"ok": True})

    chat_id = str(msg["chat"]["id"])
    text = msg.get("text", "")
    update_id = str(update.get("update_id", ""))

    # Allowlist gate: reject unknown chat IDs when allowlist is configured
    if settings.telegram_allowed_chat_ids:
        allowed = {cid.strip() for cid in settings.telegram_allowed_chat_ids.split(",")}
        if chat_id not in allowed:
            logger.info("Chat %s not in allowlist, ignoring", chat_id)
            return JSONResponse(content={"ok": True})

    # Extract media
    media_items = _extract_telegram_media(update)
    media_urls: list[tuple[str, str]] = media_items

    # Idempotency: skip duplicate updates
    message_id = str(msg.get("message_id", ""))
    external_id = f"tg_{chat_id}_{message_id}" if message_id else ""
    if external_id:
        existing = db.query(Message).filter(Message.external_message_id == external_id).first()
        if existing:
            logger.info("Duplicate webhook for update_id=%s, skipping", update_id)
            return JSONResponse(content={"ok": True})

    contractor = _get_or_create_contractor(db, chat_id)
    conversation = _get_or_create_conversation(db, contractor)

    message = Message(
        conversation_id=conversation.id,
        direction="inbound",
        external_message_id=external_id or None,
        body=text,
        media_urls_json=json.dumps([file_id for file_id, _mime in media_items]),
    )
    db.add(message)
    db.commit()
    db.refresh(message)

    task = BackgroundTask(
        _process_message_background,
        db=db,
        contractor=contractor,
        message=message,
        media_urls=media_urls,
        messaging_service=messaging_service,
    )
    return JSONResponse(content={"ok": True}, background=task)
