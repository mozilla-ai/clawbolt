"""Inbound message processing pipeline.

Each step is an independent function with clear inputs/outputs.
``handle_inbound_message`` orchestrates them via a composable pipeline
of ``PipelineStep`` callables, making it easy to add, remove, or reorder
steps without modifying the orchestrator itself.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from any_llm import AuthenticationError, ContentFilterError
from sqlalchemy.orm import Session

from backend.app.agent.context import StoredToolInteraction, load_conversation_history
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
# Pipeline context and types
# ---------------------------------------------------------------------------


@dataclass
class PipelineContext:
    """Shared state passed through pipeline steps."""

    db: Session
    contractor: Contractor
    message: Message
    media_urls: list[tuple[str, str]]
    messaging_service: MessagingService
    to_address: str = ""
    downloaded_media: list[DownloadedMedia] = field(default_factory=list)
    storage: StorageBackend | None = None
    combined_context: str = ""
    conversation_history: list[AgentMessage] = field(default_factory=list)
    system_prompt_override: str | None = None
    event_subscribers: list[Callable[[AgentEvent], Awaitable[None]]] = field(default_factory=list)
    response: AgentResponse | None = None
    _onboarding_sub: OnboardingSubscriber | None = None


PipelineStep = Callable[[PipelineContext], Awaitable[PipelineContext]]


# ---------------------------------------------------------------------------
# Pipeline steps (original functions)
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
    agent = BackshopAgent(
        db=db,
        contractor=contractor,
        messaging_service=messaging_service,
        chat_id=to_address,
    )

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

    # Note: typing indicators are sent automatically by the agent before each LLM call
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
    sent_reply = any(
        ToolTags.SENDS_REPLY in tc.get("tags", set()) and not tc.get("is_error", False)
        for tc in response.tool_calls
    )
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
    # Validate each record via StoredToolInteraction, which also strips
    # runtime-only fields like 'tags' (sets) that are not JSON-serializable.
    tool_interactions = ""
    if response.tool_calls:
        validated = [
            StoredToolInteraction.model_validate({k: v for k, v in tc.items() if k != "tags"})
            for tc in response.tool_calls
        ]
        tool_interactions = json.dumps([v.model_dump() for v in validated])

    outbound = Message(
        conversation_id=conversation_id,
        direction=MessageDirection.OUTBOUND,
        body=response.reply_text,
        tool_interactions_json=tool_interactions,
    )
    db.add(outbound)
    db.commit()


# ---------------------------------------------------------------------------
# Composable pipeline step wrappers
# ---------------------------------------------------------------------------


async def prepare_media_step(ctx: PipelineContext) -> PipelineContext:
    """Download media and initialize storage backend."""
    ctx.downloaded_media, ctx.storage = await prepare_media(
        ctx.db, ctx.contractor, ctx.message.id, ctx.media_urls, ctx.messaging_service
    )
    return ctx


async def build_context_step(ctx: PipelineContext) -> PipelineContext:
    """Run media pipeline and build combined context."""
    ctx.combined_context = await build_message_context(
        ctx.db, ctx.message, ctx.contractor, ctx.media_urls, ctx.downloaded_media
    )
    return ctx


async def load_history_step(ctx: PipelineContext) -> PipelineContext:
    """Load conversation history and set up onboarding."""
    ctx.conversation_history = await load_conversation_history(
        ctx.db, ctx.message.conversation_id, contractor_id=ctx.contractor.id
    )
    was_onboarding = is_onboarding_needed(ctx.contractor)
    if was_onboarding:
        ctx.system_prompt_override = build_onboarding_system_prompt(ctx.contractor)
    onboarding_sub = OnboardingSubscriber(ctx.db, ctx.contractor, was_onboarding)
    ctx.event_subscribers.append(onboarding_sub)
    ctx._onboarding_sub = onboarding_sub
    return ctx


async def run_agent_step(ctx: PipelineContext) -> PipelineContext:
    """Initialize agent with tools and process the message."""
    ctx.response = await run_agent(
        db=ctx.db,
        contractor=ctx.contractor,
        message=ctx.message,
        combined_context=ctx.combined_context,
        conversation_history=ctx.conversation_history,
        storage=ctx.storage,
        messaging_service=ctx.messaging_service,
        to_address=ctx.to_address,
        downloaded_media=ctx.downloaded_media,
        system_prompt_override=ctx.system_prompt_override,
        event_subscribers=ctx.event_subscribers,
    )
    return ctx


async def finalize_onboarding_step(ctx: PipelineContext) -> PipelineContext:
    """Append onboarding completion note if applicable."""
    if ctx._onboarding_sub and ctx.response:
        ctx._onboarding_sub.finalize(ctx.response)
    return ctx


async def dispatch_reply_step(ctx: PipelineContext) -> PipelineContext:
    """Send reply to the contractor."""
    if ctx.response:
        await dispatch_reply(ctx.response, ctx.messaging_service, ctx.to_address, ctx.message.id)
    return ctx


async def persist_outbound_step(ctx: PipelineContext) -> PipelineContext:
    """Store outbound message record."""
    if ctx.response:
        persist_outbound(ctx.db, ctx.message.conversation_id, ctx.response)
    return ctx


# ---------------------------------------------------------------------------
# Pipeline runner and default pipeline
# ---------------------------------------------------------------------------


async def run_pipeline(ctx: PipelineContext, steps: list[PipelineStep]) -> PipelineContext:
    """Execute pipeline steps in sequence."""
    for step in steps:
        ctx = await step(ctx)
    return ctx


DEFAULT_PIPELINE: list[PipelineStep] = [
    prepare_media_step,
    build_context_step,
    load_history_step,
    run_agent_step,
    finalize_onboarding_step,
    dispatch_reply_step,
    persist_outbound_step,
]


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


async def handle_inbound_message(
    db: Session,
    contractor: Contractor,
    message: Message,
    media_urls: list[tuple[str, str]],
    messaging_service: MessagingService,
    pipeline: list[PipelineStep] | None = None,
) -> AgentResponse:
    """Full message processing pipeline.

    Orchestrates discrete pipeline steps via a composable list of
    ``PipelineStep`` callables. Pass a custom ``pipeline`` to add,
    remove, or reorder steps; defaults to ``DEFAULT_PIPELINE``.
    """
    to_address = contractor.channel_identifier or contractor.phone
    if not to_address:
        logger.error(
            "Contractor %d has no channel_identifier or phone -- cannot send replies",
            contractor.id,
        )
        return AgentResponse(reply_text="")

    ctx = PipelineContext(
        db=db,
        contractor=contractor,
        message=message,
        media_urls=media_urls,
        messaging_service=messaging_service,
        to_address=to_address,
    )
    ctx = await run_pipeline(ctx, pipeline or DEFAULT_PIPELINE)
    return ctx.response or AgentResponse(reply_text="")
