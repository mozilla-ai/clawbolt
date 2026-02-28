import logging

from sqlalchemy.orm import Session

from backend.app.agent.context import load_conversation_history
from backend.app.agent.core import AgentResponse, BackshopAgent
from backend.app.agent.onboarding import (
    build_onboarding_system_prompt,
    extract_profile_updates,
    is_onboarding_needed,
)
from backend.app.agent.profile import update_contractor_profile
from backend.app.agent.tools.estimate_tools import create_estimate_tools
from backend.app.agent.tools.memory_tools import create_memory_tools
from backend.app.agent.tools.twilio_tools import create_twilio_tools
from backend.app.media.download import DownloadedMedia, download_twilio_media
from backend.app.media.pipeline import process_message_media
from backend.app.models import Contractor, Message
from backend.app.services.twilio_service import TwilioService

logger = logging.getLogger(__name__)


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
    media_notes: list[str] = []
    if media_urls and not downloaded_media:
        media_notes.append(
            "I couldn't download your attachment(s). The rest of your message came through fine."
        )

    try:
        pipeline_result = await process_message_media(message.body, downloaded_media)
    except Exception:
        logger.exception(
            "Media pipeline failed for message %d, contractor %d",
            message.id,
            contractor.id,
        )
        pipeline_result = await process_message_media(message.body, [])
        if downloaded_media:
            media_notes.append(
                "I received your media but couldn't process it right now. "
                "I can still help with your text message."
            )

    # Step 3: Combined context (with any media failure notes)
    combined_context = pipeline_result.combined_context
    if media_notes:
        combined_context += "\n\n[System note: " + " ".join(media_notes) + "]"

    # Step 4: Load conversation history
    conversation_history = await load_conversation_history(db, message.conversation_id)

    # Step 5: Initialize agent with tools
    onboarding = is_onboarding_needed(contractor)
    system_prompt_override = build_onboarding_system_prompt(contractor) if onboarding else None

    agent = BackshopAgent(db=db, contractor=contractor)
    tools = create_memory_tools(db, contractor.id)
    tools.extend(create_twilio_tools(twilio_service, to_number=contractor.phone))
    tools.extend(create_estimate_tools(db, contractor))
    agent.register_tools(tools)

    # Step 6: Process message through agent (with LLM failure fallback)
    try:
        response = await agent.process_message(
            message_context=combined_context,
            conversation_history=conversation_history,
            system_prompt_override=system_prompt_override,
        )
    except Exception:
        logger.exception(
            "Agent processing failed for message %d, contractor %d",
            message.id,
            contractor.id,
        )
        response = AgentResponse(
            reply_text="I'm having trouble thinking right now. Can you try again in a moment?"
        )

    # Step 6b: If onboarding, extract profile updates from tool calls
    if onboarding:
        profile_updates = extract_profile_updates(response)
        if profile_updates:
            await update_contractor_profile(db, contractor, profile_updates)

    # Step 7: If agent didn't explicitly call send_reply, send the reply text
    sent_reply = any(tc.get("name") == "send_reply" for tc in response.tool_calls)
    if not sent_reply and response.reply_text:
        try:
            await twilio_service.send_sms(to=contractor.phone, body=response.reply_text)
        except Exception:
            logger.exception(
                "Failed to send reply SMS to %s for message %d",
                contractor.phone,
                message.id,
            )

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
