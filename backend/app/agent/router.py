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

from backend.app.agent.context import load_conversation_history
from backend.app.agent.core import AgentResponse, ClawboltAgent
from backend.app.agent.events import AgentEvent
from backend.app.agent.file_store import (
    SessionState,
    StoredMessage,
    ToolConfigStore,
    UserData,
    get_session_store,
)
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
    create_list_capabilities_tool,
    default_registry,
    ensure_tool_modules_imported,
)
from backend.app.config import settings
from backend.app.enums import MessageDirection
from backend.app.media.download import DownloadedMedia
from backend.app.media.pipeline import process_message_media
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
    "The user may have sent a photo or document that could "
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

    user: UserData
    session: SessionState
    message: StoredMessage
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
    channel: str = ""
    request_id: str = ""


PipelineStep = Callable[[PipelineContext], Awaitable[PipelineContext]]


# ---------------------------------------------------------------------------
# Pipeline steps (original functions)
# ---------------------------------------------------------------------------


def init_storage(user: UserData) -> StorageBackend | None:
    """Initialize storage backend for a user.

    Returns the storage backend if configured, or ``None`` otherwise.
    """
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
            return get_storage_service(user=user)
    except Exception:
        logger.debug("Storage not configured, skipping file features")
    return None


async def prepare_media(
    user: UserData,
    message: StoredMessage,
    media_urls: list[tuple[str, str]],
    messaging_service: MessagingService,
) -> tuple[list[DownloadedMedia], StorageBackend | None]:
    """Download media and initialize storage backend.

    Returns (downloaded_media, storage_backend).
    Also auto-saves downloaded media to storage when available.
    """
    downloaded_media: list[DownloadedMedia] = []
    logger.debug(
        "Preparing media for user %d, message seq=%d: %d attachment(s)",
        user.id,
        message.seq,
        len(media_urls),
    )
    for file_id, _mime_type in media_urls:
        try:
            media = await messaging_service.download_media(file_id)
            downloaded_media.append(media)
            logger.debug("Downloaded media %s (%s)", file_id, _mime_type)
        except Exception:
            logger.exception("Failed to download media: %s", file_id)

    storage = init_storage(user)

    # Auto-save inbound media to storage
    if storage and downloaded_media:
        try:
            await auto_save_media(user, storage, downloaded_media)
        except Exception:
            logger.debug("Auto-save to storage failed, continuing")

    return downloaded_media, storage


async def build_message_context(
    session: SessionState,
    message: StoredMessage,
    user: UserData,
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
            "Media pipeline failed for message seq %d, user %d",
            message.seq,
            user.id,
        )
        pipeline_result = await process_message_media(message.body, [])
        if downloaded_media:
            media_notes.append(VISION_UNAVAILABLE_NOTE)

    combined_context = pipeline_result.combined_context
    if media_notes:
        combined_context += "\n\n[System note: " + " ".join(media_notes) + "]"

    # Persist processed context for conversation history
    session_store = get_session_store(user.id)
    await session_store.update_message(session, message.seq, processed_context=combined_context)
    message.processed_context = combined_context

    return combined_context


async def run_agent(
    user: UserData,
    message: StoredMessage,
    combined_context: str,
    conversation_history: list[AgentMessage],
    storage: StorageBackend | None,
    messaging_service: MessagingService,
    to_address: str,
    downloaded_media: list[DownloadedMedia],
    system_prompt_override: str | None = None,
    event_subscribers: list[Callable[[AgentEvent], Awaitable[None]]] | None = None,
    session_id: str = "",
) -> AgentResponse:
    """Initialize agent with tools and process the message.

    Handles LLM-level errors (content filter, auth, unexpected) by returning
    an error fallback AgentResponse.
    """
    tool_context = ToolContext(
        user=user,
        storage=storage,
        messaging_service=messaging_service,
        to_address=to_address,
        downloaded_media=downloaded_media,
    )

    agent = ClawboltAgent(
        user=user,
        messaging_service=messaging_service,
        chat_id=to_address,
        tool_context=tool_context,
        registry=default_registry,
        session_id=session_id,
    )

    # Load user's disabled tool groups to filter out unwanted tools.
    tool_config_store = ToolConfigStore(user.id)
    disabled_groups = await tool_config_store.get_disabled_tool_names()

    # Start with core tools only; specialist tools are discovered on demand
    # via the list_capabilities meta-tool. Exclude user-disabled groups.
    tools = default_registry.create_core_tools(
        tool_context, excluded_factories=disabled_groups or None
    )
    specialist_summaries = default_registry.get_available_specialist_summaries(
        tool_context, excluded_factories=disabled_groups or None
    )
    if specialist_summaries:
        tools.append(create_list_capabilities_tool(specialist_summaries))
    agent.register_tools(tools)
    logger.debug(
        "Agent initialized for user %d, message seq=%d with %d core tools, "
        "%d specialist categories available",
        user.id,
        message.seq,
        len(tools),
        len(specialist_summaries),
    )

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
            "Content filter blocked message seq %d for user %d",
            message.seq,
            user.id,
        )
        return AgentResponse(reply_text=CONTENT_FILTER_FALLBACK, is_error_fallback=True)
    except AuthenticationError:
        logger.critical(
            "LLM authentication failed processing message seq %d for user %d",
            message.seq,
            user.id,
        )
        return AgentResponse(reply_text=AUTH_ERROR_FALLBACK, is_error_fallback=True)
    except Exception:
        logger.exception(
            "Agent processing failed for message seq %d, user %d",
            message.seq,
            user.id,
        )
        return AgentResponse(reply_text=AGENT_ERROR_FALLBACK, is_error_fallback=True)


async def dispatch_reply(
    response: AgentResponse,
    messaging_service: MessagingService,
    to_address: str,
    message_seq: int,
) -> None:
    """Send reply to the user unless the agent already sent one via a tool."""
    sent_reply = any(
        ToolTags.SENDS_REPLY in tc.tags and not tc.is_error for tc in response.tool_calls
    )
    if sent_reply:
        logger.debug("Reply already sent via tool, skipping dispatch for message %d", message_seq)
    elif not response.reply_text:
        logger.debug("No reply text to dispatch for message %d", message_seq)
    if not sent_reply and response.reply_text:
        logger.debug(
            "Dispatching reply to %s for message %d (length=%d)",
            to_address,
            message_seq,
            len(response.reply_text),
        )
        try:
            await messaging_service.send_text(to=to_address, body=response.reply_text)
        except Exception:
            logger.exception(
                "Failed to send reply to %s for message seq %d",
                to_address,
                message_seq,
            )


async def persist_outbound(
    session: SessionState,
    user_id: int,
    response: AgentResponse,
) -> None:
    """Store the outbound message record.

    Skips error fallbacks to avoid poisoning conversation history.
    """
    if not response.reply_text or response.is_error_fallback:
        return

    # Serialize tool interactions for conversation history reconstruction.
    # model_dump() automatically excludes 'tags' (Field(exclude=True)).
    tool_interactions = ""
    if response.tool_calls:
        tool_interactions = json.dumps([tc.model_dump() for tc in response.tool_calls])

    session_store = get_session_store(user_id)
    await session_store.add_message(
        session=session,
        direction=MessageDirection.OUTBOUND,
        body=response.reply_text,
        tool_interactions_json=tool_interactions,
    )


# ---------------------------------------------------------------------------
# Composable pipeline step wrappers
# ---------------------------------------------------------------------------


async def prepare_media_step(ctx: PipelineContext) -> PipelineContext:
    """Download media and initialize storage backend."""
    ctx.downloaded_media, ctx.storage = await prepare_media(
        ctx.user, ctx.message, ctx.media_urls, ctx.messaging_service
    )
    return ctx


async def build_context_step(ctx: PipelineContext) -> PipelineContext:
    """Run media pipeline and build combined context."""
    ctx.combined_context = await build_message_context(
        ctx.session, ctx.message, ctx.user, ctx.media_urls, ctx.downloaded_media
    )
    return ctx


async def load_history_step(ctx: PipelineContext) -> PipelineContext:
    """Load conversation history and set up onboarding."""
    ctx.conversation_history = await load_conversation_history(ctx.session, user_id=ctx.user.id)
    was_onboarding = is_onboarding_needed(ctx.user)
    if was_onboarding:
        ctx.system_prompt_override = build_onboarding_system_prompt(ctx.user)
    onboarding_sub = OnboardingSubscriber(ctx.user, was_onboarding)
    ctx.event_subscribers.append(onboarding_sub)
    ctx._onboarding_sub = onboarding_sub
    return ctx


async def run_agent_step(ctx: PipelineContext) -> PipelineContext:
    """Initialize agent with tools and process the message."""
    ctx.response = await run_agent(
        user=ctx.user,
        message=ctx.message,
        combined_context=ctx.combined_context,
        conversation_history=ctx.conversation_history,
        storage=ctx.storage,
        messaging_service=ctx.messaging_service,
        to_address=ctx.to_address,
        downloaded_media=ctx.downloaded_media,
        system_prompt_override=ctx.system_prompt_override,
        event_subscribers=ctx.event_subscribers,
        session_id=ctx.session.session_id,
    )
    return ctx


async def finalize_onboarding_step(ctx: PipelineContext) -> PipelineContext:
    """Append onboarding completion note if applicable."""
    if ctx._onboarding_sub and ctx.response:
        ctx._onboarding_sub.finalize(ctx.response)
    return ctx


async def dispatch_reply_step(ctx: PipelineContext) -> PipelineContext:
    """Send reply: via bus when routed through a channel, or directly otherwise.

    Messages that arrive through the message bus have ``ctx.channel`` set and
    are dispatched via the bus so the outbound dispatcher can route them to the
    correct channel or resolve a web chat SSE future.

    Direct pipeline calls (heartbeat, tests) have no ``ctx.channel`` and use
    the legacy ``dispatch_reply`` path for backward compatibility.
    """
    if ctx.response:
        if ctx.channel:
            from backend.app.bus import OutboundMessage, message_bus

            sent_reply = any(
                ToolTags.SENDS_REPLY in tc.tags and not tc.is_error
                for tc in ctx.response.tool_calls
            )
            if not sent_reply and ctx.response.reply_text:
                outbound = OutboundMessage(
                    channel=ctx.channel,
                    chat_id=ctx.to_address,
                    content=ctx.response.reply_text,
                    request_id=ctx.request_id,
                )
                await message_bus.publish_outbound(outbound)
        else:
            await dispatch_reply(
                ctx.response, ctx.messaging_service, ctx.to_address, ctx.message.seq
            )
    return ctx


async def persist_outbound_step(ctx: PipelineContext) -> PipelineContext:
    """Store outbound message record."""
    if ctx.response:
        await persist_outbound(ctx.session, ctx.user.id, ctx.response)
    return ctx


# ---------------------------------------------------------------------------
# Pipeline runner and default pipeline
# ---------------------------------------------------------------------------


async def run_pipeline(ctx: PipelineContext, steps: list[PipelineStep]) -> PipelineContext:
    """Execute pipeline steps in sequence."""
    for step in steps:
        step_name = getattr(step, "__name__", type(step).__name__)
        logger.debug("Pipeline step starting: %s", step_name)
        ctx = await step(ctx)
        logger.debug("Pipeline step completed: %s", step_name)
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
    user: UserData,
    session: SessionState,
    message: StoredMessage,
    media_urls: list[tuple[str, str]],
    messaging_service: MessagingService,
    pipeline: list[PipelineStep] | None = None,
    downloaded_media: list[DownloadedMedia] | None = None,
    channel: str = "",
    request_id: str = "",
) -> AgentResponse:
    """Full message processing pipeline.

    Orchestrates discrete pipeline steps via a composable list of
    ``PipelineStep`` callables. Pass a custom ``pipeline`` to add,
    remove, or reorder steps; defaults to ``DEFAULT_PIPELINE``.
    """
    logger.debug(
        "Handling inbound message seq=%d for user %d, %d media attachment(s)",
        message.seq,
        user.id,
        len(media_urls),
    )
    to_address = user.channel_identifier or user.phone
    if not to_address:
        logger.error(
            "User %d has no channel_identifier or phone -- cannot send replies",
            user.id,
        )
        return AgentResponse(reply_text="")

    ctx = PipelineContext(
        user=user,
        session=session,
        message=message,
        media_urls=media_urls,
        messaging_service=messaging_service,
        to_address=to_address,
        downloaded_media=downloaded_media or [],
        channel=channel,
        request_id=request_id,
    )
    ctx = await run_pipeline(ctx, pipeline or DEFAULT_PIPELINE)
    return ctx.response or AgentResponse(reply_text="")
