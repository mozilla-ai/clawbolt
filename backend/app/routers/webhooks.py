import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.orm import Session
from twilio.request_validator import RequestValidator

from backend.app.agent.router import handle_inbound_message
from backend.app.config import settings
from backend.app.database import get_db
from backend.app.models import Contractor, Conversation, Message
from backend.app.services.twilio_service import TwilioService, get_twilio_service

logger = logging.getLogger(__name__)

router = APIRouter()

TWIML_EMPTY = '<?xml version="1.0" encoding="UTF-8"?><Response/>'


def _validate_twilio_signature(request: Request, form_data: dict[str, str]) -> None:
    """Validate the Twilio request signature if enabled."""
    if not settings.twilio_validate_signatures:
        return
    signature = request.headers.get("X-Twilio-Signature", "")
    validator = RequestValidator(settings.twilio_auth_token)
    url = str(request.url)
    if not validator.validate(url, form_data, signature):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")


def _extract_media(form_data: dict[str, str]) -> list[dict[str, str]]:
    """Extract media URLs and content types from Twilio webhook payload."""
    num_media = int(form_data.get("NumMedia", "0"))
    media: list[dict[str, str]] = []
    for i in range(num_media):
        url = form_data.get(f"MediaUrl{i}", "")
        content_type = form_data.get(f"MediaContentType{i}", "")
        if url:
            media.append({"url": url, "content_type": content_type})
    return media


def _get_or_create_contractor(db: Session, phone: str) -> Contractor:
    """Look up or create a contractor by phone number."""
    contractor = db.query(Contractor).filter(Contractor.phone == phone).first()
    if contractor is None:
        contractor = Contractor(
            user_id=phone,
            phone=phone,
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


@router.post("/webhooks/twilio/inbound")
async def twilio_inbound(
    request: Request,
    db: Session = Depends(get_db),
    twilio_service: TwilioService = Depends(get_twilio_service),
) -> Response:
    """Receive inbound SMS/MMS from Twilio."""
    form_data = dict(await request.form())
    # Ensure all values are strings
    form_data = {k: str(v) for k, v in form_data.items()}

    _validate_twilio_signature(request, form_data)

    phone = form_data.get("From", "")
    body = form_data.get("Body", "")
    media = _extract_media(form_data)
    media_urls: list[tuple[str, str]] = [(m["url"], m["content_type"]) for m in media]

    contractor = _get_or_create_contractor(db, phone)
    conversation = _get_or_create_conversation(db, contractor)

    message = Message(
        conversation_id=conversation.id,
        direction="inbound",
        body=body,
        media_urls_json=json.dumps([url for url, _ct in media_urls]),
    )
    db.add(message)
    db.commit()
    db.refresh(message)

    # Process through agent pipeline (media → LLM → reply SMS)
    try:
        await handle_inbound_message(
            db=db,
            contractor=contractor,
            message=message,
            media_urls=media_urls,
            twilio_service=twilio_service,
        )
    except Exception:
        logger.exception(
            "Agent pipeline failed for message %d from %s",
            message.id,
            phone,
        )

    return Response(content=TWIML_EMPTY, media_type="application/xml")
