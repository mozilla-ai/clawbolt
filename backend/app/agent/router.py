import logging

from sqlalchemy.orm import Session

from backend.app.agent.core import AgentResponse, BackshopAgent
from backend.app.agent.tools.memory_tools import create_memory_tools
from backend.app.agent.tools.twilio_tools import create_twilio_tools
from backend.app.media.download import DownloadedMedia, download_twilio_media
from backend.app.media.pipeline import process_message_media
from backend.app.models import Contractor, Message
from backend.app.services.twilio_service import TwilioService

logger = logging.getLogger(__name__)

HISTORY_LIMIT = 20


async def handle_inbound_message(
    db: Session,
    contractor: Contractor,
    message: Message,
    media_urls: list[tuple[str, str]],
    twilio_service: TwilioService,
) -> AgentResponse:
    """Full message processing pipeline.

    1. Download media from Twilio URLs (if any)
    2. Run media pipeline (vision, audio, PDF extraction)
    3. Build combined context (text + processed media)
    4. Load conversation history
    5. Initialize agent with tools
    6. Process message through agent
    7. Agent sends reply via tools or returns reply text
    """
    # Step 1: Download media
    downloaded_media: list[DownloadedMedia] = []
    for url, _mime_type in media_urls:
        try:
            media = await download_twilio_media(url)
            downloaded_media.append(media)
        except Exception:
            logger.exception("Failed to download media: %s", url)

    # Step 2: Run media pipeline
    pipeline_result = await process_message_media(message.body, downloaded_media)

    # Step 3: Combined context is ready in pipeline_result.combined_context

    # Step 4: Load conversation history
    conversation_history = _load_conversation_history(db, message.conversation_id)

    # Step 5: Initialize agent with tools
    agent = BackshopAgent(db=db, contractor=contractor)
    tools = create_memory_tools(db, contractor.id)
    tools.extend(create_twilio_tools(twilio_service, to_number=contractor.phone))
    agent.register_tools(tools)

    # Step 6: Process message
    response = await agent.process_message(
        message_context=pipeline_result.combined_context,
        conversation_history=conversation_history,
    )

    # Step 7: If agent didn't explicitly call send_reply, send the reply text
    sent_reply = any(tc.get("name") == "send_reply" for tc in response.tool_calls)
    if not sent_reply and response.reply_text:
        try:
            await twilio_service.send_sms(to=contractor.phone, body=response.reply_text)
        except Exception:
            logger.exception("Failed to send reply SMS")

    # Store outbound message
    if response.reply_text:
        outbound = Message(
            conversation_id=message.conversation_id,
            direction="outbound",
            body=response.reply_text,
        )
        db.add(outbound)
        db.commit()

    return response


def _load_conversation_history(db: Session, conversation_id: int) -> list[dict[str, str]]:
    """Load recent messages from the conversation for context."""
    messages = (
        db.query(Message)
        .filter(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.desc())
        .limit(HISTORY_LIMIT)
        .all()
    )
    # Reverse to chronological order, skip the current (most recent) message
    messages = list(reversed(messages))[:-1] if len(messages) > 1 else []

    history: list[dict[str, str]] = []
    for msg in messages:
        role = "user" if msg.direction == "inbound" else "assistant"
        history.append({"role": role, "content": msg.body})
    return history
