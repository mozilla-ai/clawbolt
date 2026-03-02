"""Telegram webhook endpoint for inbound messages."""

import json
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from backend.app.agent.context import get_or_create_conversation
from backend.app.agent.router import handle_inbound_message
from backend.app.config import settings
from backend.app.database import SessionLocal, get_db
from backend.app.models import Contractor, Message
from backend.app.services.messaging import MessagingService, get_messaging_service
from backend.app.services.rate_limiter import check_webhook_rate_limit

logger = logging.getLogger(__name__)

TELEGRAM_SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"

router = APIRouter()


def _validate_webhook_secret(request: Request) -> None:
    """Validate the Telegram webhook secret token header."""
    if not settings.telegram_webhook_secret:
        return
    header = request.headers.get(TELEGRAM_SECRET_HEADER, "")
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
        file_id = largest.get("file_id")
        if file_id:
            media.append((file_id, "image/jpeg"))

    # Voice note
    voice = msg.get("voice")
    if voice:
        file_id = voice.get("file_id")
        if file_id:
            media.append((file_id, voice.get("mime_type", "audio/ogg")))

    # Document — preserve Telegram-provided MIME type so images sent as
    # documents (e.g. image/png) are correctly classified downstream
    doc = msg.get("document")
    if doc:
        file_id = doc.get("file_id")
        if file_id:
            doc_mime = doc.get("mime_type", "application/octet-stream")
            media.append((file_id, doc_mime))

    if media:
        logger.debug(
            "Extracted %d media item(s): %s",
            len(media),
            [(file_id, mime_type) for file_id, mime_type in media],
        )
    return media


@router.post("/webhooks/telegram")
async def telegram_inbound(
    request: Request,
    _rate_limit: None = Depends(check_webhook_rate_limit),
    db: Session = Depends(get_db),
    messaging_service: MessagingService = Depends(get_messaging_service),
) -> JSONResponse:
    """Receive inbound messages from Telegram."""
    try:
        _validate_webhook_secret(request)
    except _InvalidSecret:
        return JSONResponse(content={"ok": True})

    try:
        update: dict = await request.json()
    except ValueError:
        logger.warning("Telegram webhook received invalid JSON")
        return JSONResponse(content={"ok": True})

    msg = update.get("message")
    if not msg:
        # Not a message update (could be edited_message, callback_query, etc.)
        return JSONResponse(content={"ok": True})

    chat = msg.get("chat") if isinstance(msg, dict) else None
    if not chat or "id" not in chat:
        logger.warning("Telegram message missing chat.id, ignoring")
        return JSONResponse(content={"ok": True})
    chat_id = str(chat["id"])
    text = msg.get("text", "")
    update_id = str(update.get("update_id", ""))

    # Allowlist gate: reject messages when allowlists are configured and neither matches
    username = msg.get("from", {}).get("username", "")
    chat_id_match = False
    username_match = False

    if settings.telegram_allowed_chat_ids:
        allowed_ids = {cid.strip() for cid in settings.telegram_allowed_chat_ids.split(",")}
        chat_id_match = chat_id in allowed_ids

    if settings.telegram_allowed_usernames:
        allowed_users = {
            u.strip().lstrip("@").lower() for u in settings.telegram_allowed_usernames.split(",")
        }
        username_match = username.lower() in allowed_users if username else False

    # If any allowlist is configured, at least one must match (OR logic)
    any_allowlist_configured = bool(
        settings.telegram_allowed_chat_ids or settings.telegram_allowed_usernames
    )
    if any_allowlist_configured and not (chat_id_match or username_match):
        logger.info("Chat %s / @%s not in allowlist, ignoring", chat_id, username)
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
    conversation, _is_new = await get_or_create_conversation(db, contractor.id)

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
        contractor_id=contractor.id,
        message_id=message.id,
        media_urls=media_urls,
        messaging_service=messaging_service,
    )
    return JSONResponse(content={"ok": True}, background=task)
