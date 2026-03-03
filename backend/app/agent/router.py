import logging

from any_llm import AuthenticationError, ContentFilterError
from sqlalchemy.orm import Session

from backend.app.agent.context import load_conversation_history
from backend.app.agent.core import AgentResponse, BackshopAgent
from backend.app.agent.onboarding import (
    build_onboarding_system_prompt,
    extract_profile_updates,
    is_onboarding_needed,
)
from backend.app.agent.profile import update_contractor_profile
from backend.app.agent.tools.base import ToolTags
from backend.app.agent.tools.checklist_tools import create_checklist_tools
from backend.app.agent.tools.estimate_tools import create_estimate_tools
from backend.app.agent.tools.file_tools import auto_save_media, create_file_tools
from backend.app.agent.tools.memory_tools import create_memory_tools
from backend.app.agent.tools.messaging_tools import create_messaging_tools
from backend.app.config import settings
from backend.app.media.download import DownloadedMedia, download_telegram_media
from backend.app.media.pipeline import process_message_media
from backend.app.models import Contractor, Message
from backend.app.services.messaging import MessagingService
from backend.app.services.storage_service import StorageBackend, get_storage_service

logger = logging.getLogger(__name__)

# User-facing error/fallback messages
AGENT_ERROR_FALLBACK = "I'm having trouble thinking right now. Can you try again in a moment?"
CONTENT_FILTER_FALLBACK = "I wasn't able to process that message. Could you try rephrasing?"
AUTH_ERROR_FALLBACK = (
    "I'm experiencing a configuration issue and can't respond right now. "
    "The admin has been notified."
)
MEDIA_DOWNLOAD_ERROR = (
    "I couldn't download your attachment(s). The rest of your message came through fine."
)
VISION_UNAVAILABLE_NOTE = (
    "Vision analysis was unavailable for the attached media. "
    "The contractor may have sent a photo or document that could "
    "not be analyzed. You can still help with their text message "
    "and ask them to describe what the attachment shows."
)


async def handle_inbound_message(
    db: Session,
    contractor: Contractor,
    message: Message,
    media_urls: list[tuple[str, str]],
    messaging_service: MessagingService,
) -> AgentResponse:
    """Full message processing pipeline.

    1. Download media (if any)
    2. Run media pipeline (vision, audio, PDF extraction)
    3. Build combined context (text + processed media)
    4. Load conversation history
    5. Initialize agent with tools
    6. Process message through agent
    7. Agent sends reply via tools or returns reply text
    """
    to_address = contractor.channel_identifier or contractor.phone
    if not to_address:
        logger.error(
            "Contractor %d has no channel_identifier or phone -- cannot send replies",
            contractor.id,
        )
        return AgentResponse(reply_text="")

    # Step 1: Download media
    downloaded_media: list[DownloadedMedia] = []
    for file_id, _mime_type in media_urls:
        try:
            media = await download_telegram_media(file_id)
            downloaded_media.append(media)
        except Exception:
            logger.exception("Failed to download media: %s", file_id)

    # Initialize storage backend (used for auto-save and tools)
    storage: StorageBackend | None = None
    try:
        has_storage = (
            settings.storage_provider == "local"
            or (settings.storage_provider == "dropbox" and settings.dropbox_access_token)
            or (
                settings.storage_provider == "google_drive"
                and settings.google_drive_credentials_json
            )
        )
        if has_storage:
            storage = get_storage_service(contractor=contractor)
    except Exception:
        logger.debug("Storage not configured, skipping file features")

    # Step 1.5: Auto-save inbound media to storage
    if storage and downloaded_media:
        try:
            await auto_save_media(db, contractor, storage, downloaded_media, message_id=message.id)
        except Exception:
            logger.debug("Auto-save to storage failed, continuing")

    # Step 2: Run media pipeline
    media_notes: list[str] = []
    if media_urls and not downloaded_media:
        media_notes.append(MEDIA_DOWNLOAD_ERROR)

    try:
        pipeline_result = await process_message_media(
            message.body, downloaded_media, user=str(contractor.id)
        )
    except Exception:
        logger.exception(
            "Media pipeline failed for message %d, contractor %d",
            message.id,
            contractor.id,
        )
        pipeline_result = await process_message_media(message.body, [])
        if downloaded_media:
            media_notes.append(VISION_UNAVAILABLE_NOTE)

    # Step 3: Combined context (with any media failure notes)
    combined_context = pipeline_result.combined_context
    if media_notes:
        combined_context += "\n\n[System note: " + " ".join(media_notes) + "]"

    # Persist processed context for conversation history
    message.processed_context = combined_context
    db.commit()

    # Step 4: Load conversation history
    conversation_history = await load_conversation_history(db, message.conversation_id)

    # Step 5: Initialize agent with tools
    was_onboarding = is_onboarding_needed(contractor)
    system_prompt_override = build_onboarding_system_prompt(contractor) if was_onboarding else None

    agent = BackshopAgent(db=db, contractor=contractor)
    tools = create_memory_tools(db, contractor.id)
    tools.extend(create_messaging_tools(messaging_service, to_address=to_address))
    tools.extend(create_estimate_tools(db, contractor, storage))
    tools.extend(create_checklist_tools(db, contractor.id))

    # Wire file tools if storage is available
    if storage:
        pending_media = {m.original_url: m.content for m in downloaded_media if m.content}
        tools.extend(create_file_tools(db, contractor, storage, pending_media))

    agent.register_tools(tools)

    # Send typing indicator while processing (non-blocking on failure)
    try:
        await messaging_service.send_typing_indicator(to=to_address)
    except Exception:
        logger.debug("Failed to send typing indicator to %s", to_address)

    # Step 6: Process message through agent (with LLM failure fallback)
    try:
        response = await agent.process_message(
            message_context=combined_context,
            conversation_history=conversation_history,
            system_prompt_override=system_prompt_override,
        )
    except ContentFilterError:
        logger.warning(
            "Content filter blocked message %d for contractor %d",
            message.id,
            contractor.id,
        )
        response = AgentResponse(reply_text=CONTENT_FILTER_FALLBACK, is_error_fallback=True)
    except AuthenticationError:
        logger.critical(
            "LLM authentication failed processing message %d for contractor %d",
            message.id,
            contractor.id,
        )
        response = AgentResponse(reply_text=AUTH_ERROR_FALLBACK, is_error_fallback=True)
    except Exception:
        logger.exception(
            "Agent processing failed for message %d, contractor %d",
            message.id,
            contractor.id,
        )
        response = AgentResponse(reply_text=AGENT_ERROR_FALLBACK, is_error_fallback=True)

    # Step 6b: Always extract profile updates from tool calls (not just during
    # onboarding).  This keeps contractor profile fields in sync with memory
    # facts when contractors update their info post-onboarding (e.g., "I moved
    # to Denver", "my new rate is $100/hr").  Fixes #186 / #183.
    profile_updates = extract_profile_updates(response)
    if profile_updates:
        await update_contractor_profile(db, contractor, profile_updates)
        db.refresh(contractor)

    # If still onboarding, check whether the required fields are now complete
    if was_onboarding and not is_onboarding_needed(contractor):
        contractor.onboarding_complete = True
        db.commit()

        # Append completion summary when onboarding transitions to complete
        if contractor.onboarding_complete:
            parts = [f"Name: {contractor.name}", f"Trade: {contractor.trade}"]
            if contractor.location:
                parts.append(f"Location: {contractor.location}")
            if contractor.hourly_rate:
                parts.append(f"Rate: ${contractor.hourly_rate:.0f}/hour")
            summary = "\n".join(f"- {p}" for p in parts)
            completion_note = (
                "\n\nSetup complete! Here's what I know about you:\n"
                f"{summary}\n\n"
                "You can update any of this anytime. I'm ready to help!"
            )
            if response.reply_text:
                response.reply_text += completion_note

    # Step 6c: Ensure onboarding_complete is set when required fields are already satisfied
    # (e.g. pre-populated contractors that skipped the onboarding flow)
    if not contractor.onboarding_complete and not is_onboarding_needed(contractor):
        contractor.onboarding_complete = True
        db.commit()

    # Step 7: If agent didn't explicitly call a reply tool, send the reply text
    sent_reply = any(ToolTags.SENDS_REPLY in tc.get("tags", set()) for tc in response.tool_calls)
    if not sent_reply and response.reply_text:
        try:
            await messaging_service.send_text(to=to_address, body=response.reply_text)
        except Exception:
            logger.exception(
                "Failed to send reply to %s for message %d",
                to_address,
                message.id,
            )

    # Store outbound message (skip error fallbacks to avoid poisoning
    # conversation history -- the LLM would see the error on subsequent turns)
    if response.reply_text and not response.is_error_fallback:
        outbound = Message(
            conversation_id=message.conversation_id,
            direction="outbound",
            body=response.reply_text,
        )
        db.add(outbound)
        db.commit()

    return response
