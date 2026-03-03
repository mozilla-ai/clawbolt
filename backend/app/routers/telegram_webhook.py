"""Telegram webhook endpoint for inbound messages."""

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from backend.app.agent.ingestion import InboundMessage, process_inbound_message
from backend.app.config import get_effective_webhook_secret, settings
from backend.app.database import get_db
from backend.app.models import Message
from backend.app.services.messaging import MessagingService, get_messaging_service
from backend.app.services.rate_limiter import check_webhook_rate_limit

logger = logging.getLogger(__name__)

TELEGRAM_SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"

router = APIRouter()


def _validate_webhook_secret(request: Request) -> None:
    """Validate the Telegram webhook secret token header."""
    secret = get_effective_webhook_secret(settings)
    if not secret:
        return
    header = request.headers.get(TELEGRAM_SECRET_HEADER, "")
    if header != secret:
        logger.warning("Invalid Telegram webhook secret")
        # Still return 200 to avoid Telegram retries, but log the warning.
        # The message will not be processed (early return in the endpoint).
        raise _InvalidSecret


class _InvalidSecret(Exception):
    pass


def _extract_telegram_media(
    update: dict,
) -> list[tuple[str, str]]:
    """Extract media file_ids from a Telegram update.

    Returns a list of (file_id, mime_type) tuples.
    """
    msg = update.get("message", {})
    media: list[tuple[str, str]] = []

    # Photo: Telegram sends multiple sizes -- pick the largest
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

    # Video
    video = msg.get("video")
    if video:
        file_id = video.get("file_id")
        if file_id:
            media.append((file_id, video.get("mime_type", "video/mp4")))

    # Video note (round video messages)
    video_note = msg.get("video_note")
    if video_note:
        file_id = video_note.get("file_id")
        if file_id:
            media.append((file_id, "video/mp4"))

    # Audio file (not voice note)
    audio = msg.get("audio")
    if audio:
        file_id = audio.get("file_id")
        if file_id:
            media.append((file_id, audio.get("mime_type", "audio/mpeg")))

    # Document -- preserve Telegram-provided MIME type so images sent as
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


def _check_allowlist(chat_id: str, username: str) -> bool:
    """Return True if the sender passes the allowlist gate.

    If no allowlist is configured, all senders are allowed.
    """
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

    any_allowlist_configured = bool(
        settings.telegram_allowed_chat_ids or settings.telegram_allowed_usernames
    )
    return not any_allowlist_configured or chat_id_match or username_match


def _parse_telegram_update(update: dict) -> InboundMessage | None:
    """Parse a Telegram update dict into an InboundMessage.

    Returns None if the update should be ignored (not a message, missing
    chat.id, or fails the allowlist check).
    """
    msg = update.get("message")
    if not msg:
        return None

    chat = msg.get("chat") if isinstance(msg, dict) else None
    if not chat or "id" not in chat:
        logger.warning("Telegram message missing chat.id, ignoring")
        return None

    chat_id = str(chat["id"])
    raw_text = msg.get("text") or msg.get("caption") or ""

    # Strip Telegram bot commands: /start is sent automatically when a user
    # opens the bot for the first time. Treat it as a greeting so the agent
    # sees a natural message instead of a raw command.
    text = raw_text
    if raw_text.strip().lower() in ("/start", "/start@"):
        text = "Hi"
    elif raw_text.strip().startswith("/"):
        # Ignore other bot commands (e.g. /help, /settings) that the bot
        # does not handle. Return None to skip processing entirely.
        logger.debug("Ignoring unhandled bot command: %s", raw_text.strip().split()[0])
        return None

    username = msg.get("from", {}).get("username", "")

    if not _check_allowlist(chat_id, username):
        logger.info("Chat %s / @%s not in allowlist, ignoring", chat_id, username)
        return None

    media_items = _extract_telegram_media(update)

    message_id = str(msg.get("message_id", ""))
    external_id = f"tg_{chat_id}_{message_id}" if message_id else ""

    return InboundMessage(
        channel="telegram",
        sender_id=chat_id,
        text=text,
        media_refs=media_items,
        external_message_id=external_id,
        sender_username=username or None,
    )


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

    inbound = _parse_telegram_update(update)
    if inbound is None:
        return JSONResponse(content={"ok": True})

    # Idempotency: skip duplicate updates
    if inbound.external_message_id:
        existing = (
            db.query(Message)
            .filter(Message.external_message_id == inbound.external_message_id)
            .first()
        )
        if existing:
            logger.info("Duplicate webhook for %s, skipping", inbound.external_message_id)
            return JSONResponse(content={"ok": True})

    task, _contractor, _message = await process_inbound_message(db, inbound, messaging_service)
    return JSONResponse(content={"ok": True}, background=task)
