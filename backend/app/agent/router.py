"""Inbound message processing pipeline.

Each step is an independent function with clear inputs/outputs.
``handle_inbound_message`` orchestrates them in sequence.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable

from any_llm import AuthenticationError, ContentFilterError
from sqlalchemy.orm import Session

from backend.app.agent.context import load_conversation_history
from backend.app.agent.core import AgentResponse, BackshopAgent
from backend.app.agent.events import AgentEvent
from backend.app.agent.messages import AgentMessage
from backend.app.agent.onboarding import (
    OnboardingSubscriber,
    build_onboarding_system_prompt,
    is_onboarding_needed,
)
from backend.app.agent.tools.base import ToolTags
from backend.app.agent.tools.file_tools import auto_save_media
from backend.app.agent.tools.registry import (
    ToolContext,
    default_registry,
    ensure_tool_modules_imported,
    select_tools,
)
from backend.app.config import settings
from backend.app.enums import MessageDirection
from backend.app.media.download import DownloadedMedia
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

# Ensure all tool modules have self-registered with the default registry.
ensure_tool_modules_imported()


# ---------------------------------------------------------------------------
# Pipeline steps
# ---------------------------------------------------------------------------


async def prepare_media(
    db: Session,
    contractor: Contractor,
    message_id: int,
    media_urls: list[tuple[str, str]],
    messaging_service: MessagingService,
) -> tuple[list[DownloadedMedia], StorageBackend | None]:
    """Download media and initialize storage backend.

    Returns (downloaded_media, storage_backend).
    Also auto-saves downloaded media to storage when available.
    """
    downloaded_media: list[DownloadedMedia] = []
    for file_id, _mime_type in media_urls:
        try:
            media = await messaging_service.download_media(file_id)
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

    # Auto-save inbound media to storage
    if storage and downloaded_media:
        try:
            await auto_save_media(db, contractor, storage, downloaded_media, message_id=message_id)
        except Exception:
            logger.debug("Auto-save to storage failed, continuing")

    return downloaded_media, storage


async def build_message_context(
    db: Session,
    message: Message,
    contractor: Contractor,
    media_urls: list[tuple[str, str]],
    downloaded_media: list[DownloadedMedia],
) -> str:
    """Run media pipeline and build combined context string.

    Persists the processed context on the message record and returns it.
    """
    media_notes: list[str] = []
    if media_urls and not downloaded_media:
        media_notes.append(MEDIA_DOWNLOAD_ERROR)

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
            media_notes.append(VISION_UNAVAILABLE_NOTE)

    combined_context = pipeline_result.combined_context
    if media_notes:
        combined_context += "\n\n[System note: " + " ".join(media_notes) + "]"

    # Persist processed context for conversation history
    message.processed_context = combined_context
    db.commit()

    return combined_context


async def run_agent(
    db: Session,
    contractor: Contractor,
    message: Message,
    combined_context: str,
    conversation_history: list[AgentMessage],
    storage: StorageBackend | None,
    messaging_service: MessagingService,
    to_address: str,
    downloaded_media: list[DownloadedMedia],
    system_prompt_override: str | None = None,
    event_subscribers: list[Callable[[AgentEvent], Awaitable[None]]] | None = None,
) -> AgentResponse:
    """Initialize agent with tools and process the message.

    Handles LLM-level errors (content filter, auth, unexpected) by returning
    an error fallback AgentResponse.
    """
    agent = BackshopAgent(db=db, contractor=contractor)

    tool_context = ToolContext(
        db=db,
        contractor=contractor,
        storage=storage,
        messaging_service=messaging_service,
        to_address=to_address,
        downloaded_media=downloaded_media,
    )

    selected_factories = select_tools(
        message.body or "",
        has_media=bool(downloaded_media),
        has_storage=storage is not None,
        factory_names=default_registry.factory_names,
    )
    tools = default_registry.create_tools(tool_context, selected_factories=selected_factories)
    agent.register_tools(tools)

    for subscriber in event_subscribers or []:
        agent.subscribe(subscriber)

    # Send typing indicator while processing (non-blocking on failure)
    try:
        await messaging_service.send_typing_indicator(to=to_address)
    except Exception:
        logger.debug("Failed to send typing indicator to %s", to_address)

    try:
        return await agent.process_message(
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
        return AgentResponse(reply_text=CONTENT_FILTER_FALLBACK, is_error_fallback=True)
    except AuthenticationError:
        logger.critical(
            "LLM authentication failed processing message %d for contractor %d",
            message.id,
            contractor.id,
        )
        return AgentResponse(reply_text=AUTH_ERROR_FALLBACK, is_error_fallback=True)
    except Exception:
        logger.exception(
            "Agent processing failed for message %d, contractor %d",
            message.id,
            contractor.id,
        )
        return AgentResponse(reply_text=AGENT_ERROR_FALLBACK, is_error_fallback=True)


async def dispatch_reply(
    response: AgentResponse,
    messaging_service: MessagingService,
    to_address: str,
    message_id: int,
) -> None:
    """Send reply to the contractor unless the agent already sent one via a tool."""
    sent_reply = any(ToolTags.SENDS_REPLY in tc.get("tags", set()) for tc in response.tool_calls)
    if not sent_reply and response.reply_text:
        try:
            await messaging_service.send_text(to=to_address, body=response.reply_text)
        except Exception:
            logger.exception(
                "Failed to send reply to %s for message %d",
                to_address,
                message_id,
            )


def persist_outbound(
    db: Session,
    conversation_id: int,
    response: AgentResponse,
) -> None:
    """Store the outbound message record.

    Skips error fallbacks to avoid poisoning conversation history.
    """
    if not response.reply_text or response.is_error_fallback:
        return

    # Serialize tool interactions for conversation history reconstruction.
    # Strip non-serializable 'tags' (sets) before JSON encoding.
    tool_interactions = ""
    if response.tool_calls:
        serializable = [{k: v for k, v in tc.items() if k != "tags"} for tc in response.tool_calls]
        tool_interactions = json.dumps(serializable)

    outbound = Message(
        conversation_id=conversation_id,
        direction=MessageDirection.OUTBOUND,
        body=response.reply_text,
        tool_interactions_json=tool_interactions,
    )
    db.add(outbound)
    db.commit()


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


async def handle_inbound_message(
    db: Session,
    contractor: Contractor,
    message: Message,
    media_urls: list[tuple[str, str]],
    messaging_service: MessagingService,
) -> AgentResponse:
    """Full message processing pipeline.

    Orchestrates discrete pipeline steps:
    1. prepare_media        - download and auto-save media
    2. build_message_context - run media pipeline, build combined context
    3. run_agent            - initialize agent with tools and event subscribers
    4. finalize_onboarding  - append completion note if onboarding just finished
    5. dispatch_reply       - send reply to contractor
    6. persist_outbound     - store outbound message record
    """
    to_address = contractor.channel_identifier or contractor.phone
    if not to_address:
        logger.error(
            "Contractor %d has no channel_identifier or phone -- cannot send replies",
            contractor.id,
        )
        return AgentResponse(reply_text="")

    # 1. Download and auto-save media
    downloaded_media, storage = await prepare_media(
        db, contractor, message.id, media_urls, messaging_service
    )

    # 2. Build combined context from text + media
    combined_context = await build_message_context(
        db, message, contractor, media_urls, downloaded_media
    )

    # 3. Load history and run agent (with onboarding subscriber)
    conversation_history = await load_conversation_history(db, message.conversation_id)
    was_onboarding = is_onboarding_needed(contractor)
    system_prompt_override = build_onboarding_system_prompt(contractor) if was_onboarding else None
    onboarding_sub = OnboardingSubscriber(db, contractor, was_onboarding)
    response = await run_agent(
        db=db,
        contractor=contractor,
        message=message,
        combined_context=combined_context,
        conversation_history=conversation_history,
        storage=storage,
        messaging_service=messaging_service,
        to_address=to_address,
        downloaded_media=downloaded_media,
        system_prompt_override=system_prompt_override,
        event_subscribers=[onboarding_sub],
    )

    # 4. Finalize onboarding (append completion note if applicable)
    onboarding_sub.finalize(response)

    # 5. Send reply
    await dispatch_reply(response, messaging_service, to_address, message.id)

    # 6. Persist outbound message
    persist_outbound(db, message.conversation_id, response)

    return response
