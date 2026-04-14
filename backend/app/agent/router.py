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
from backend.app.agent.dto import SessionState, StoredMessage
from backend.app.agent.events import (
    AgentEndEvent,
    AgentEvent,
    ToolExecutionStartEvent,
    TurnStartEvent,
)
from backend.app.agent.messages import AgentMessage
from backend.app.agent.onboarding import (
    OnboardingSubscriber,
    build_onboarding_system_prompt,
    is_onboarding_needed,
)
from backend.app.agent.session_db import get_session_store
from backend.app.agent.skills.loader import load_all_skills
from backend.app.agent.stores import ToolConfigStore
from backend.app.agent.tools.base import ToolTags
from backend.app.agent.tools.file_tools import auto_save_media
from backend.app.agent.tools.registry import (
    ToolContext,
    create_list_capabilities_tool,
    default_registry,
    ensure_tool_modules_imported,
)
from backend.app.config import settings
from backend.app.database import SessionLocal
from backend.app.enums import MessageDirection
from backend.app.media.download import DownloadedMedia
from backend.app.media.pipeline import process_message_media
from backend.app.models import ChannelRoute, User
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

# Load skill documentation (SKILL.md files) for specialist categories.
load_all_skills()


# ---------------------------------------------------------------------------
# Pipeline context and types
# ---------------------------------------------------------------------------


@dataclass
class PipelineContext:
    """Shared state passed through pipeline steps."""

    user: User
    session: SessionState
    message: StoredMessage
    media_urls: list[tuple[str, str]]
    channel: str = ""
    to_address: str = ""
    downloaded_media: list[DownloadedMedia] = field(default_factory=list)
    download_media: Callable[[str], Awaitable[DownloadedMedia]] | None = None
    storage: StorageBackend | None = None
    combined_context: str = ""
    conversation_history: list[AgentMessage] = field(default_factory=list)
    system_prompt_override: str | None = None
    is_onboarding: bool = False
    event_subscribers: list[Callable[[AgentEvent], Awaitable[None]]] = field(default_factory=list)
    response: AgentResponse | None = None
    _onboarding_sub: OnboardingSubscriber | None = None
    request_id: str = ""


PipelineStep = Callable[[PipelineContext], Awaitable[PipelineContext]]


# ---------------------------------------------------------------------------
# Pipeline steps (original functions)
# ---------------------------------------------------------------------------


def init_storage(user: User) -> StorageBackend | None:
    """Initialize storage backend for a user.

    Returns the storage backend if configured, or ``None`` otherwise.
    """
    has_storage = (
        settings.storage_provider == "local"
        or (settings.storage_provider == "dropbox" and settings.dropbox_access_token)
        or (settings.storage_provider == "google_drive" and settings.google_drive_credentials_json)
    )
    if not has_storage:
        logger.debug(
            "Storage not configured (provider=%r), skipping file features",
            settings.storage_provider,
        )
        return None
    try:
        return get_storage_service(user=user)
    except Exception:
        logger.exception("Storage backend %r failed to initialize", settings.storage_provider)
    return None


async def prepare_media(
    user: User,
    message: StoredMessage,
    media_urls: list[tuple[str, str]],
    download_media: Callable[[str], Awaitable[DownloadedMedia]] | None = None,
) -> tuple[list[DownloadedMedia], StorageBackend | None]:
    """Download media and initialize storage backend.

    Returns (downloaded_media, storage_backend).
    Also auto-saves downloaded media to storage when available.
    """
    downloaded_media: list[DownloadedMedia] = []
    logger.debug(
        "Preparing media for user %s, message seq=%d: %d attachment(s)",
        user.id,
        message.seq,
        len(media_urls),
    )
    if download_media is not None:
        for file_id, _mime_type in media_urls:
            try:
                media = await download_media(file_id)
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
    user: User,
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
            "Media pipeline failed for message seq %d, user %s",
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
    user: User,
    message: StoredMessage,
    combined_context: str,
    conversation_history: list[AgentMessage],
    storage: StorageBackend | None,
    to_address: str,
    downloaded_media: list[DownloadedMedia],
    channel: str = "",
    system_prompt_override: str | None = None,
    is_onboarding: bool = False,
    event_subscribers: list[Callable[[AgentEvent], Awaitable[None]]] | None = None,
    session_id: str = "",
    request_id: str = "",
) -> AgentResponse:
    """Initialize agent with tools and process the message.

    Handles LLM-level errors (content filter, auth, unexpected) by returning
    an error fallback AgentResponse.
    """
    from backend.app.bus import message_bus

    publish_outbound = message_bus.publish_outbound if channel else None

    tool_context = ToolContext(
        user=user,
        storage=storage,
        publish_outbound=publish_outbound,
        channel=channel,
        to_address=to_address,
        downloaded_media=downloaded_media,
    )

    # Load user's disabled tool groups and individual sub-tools.
    tool_config_store = ToolConfigStore(user.id)
    disabled_groups = await tool_config_store.get_disabled_tool_names()
    disabled_sub_tools = await tool_config_store.get_disabled_sub_tool_names()

    # Ensure PERMISSIONS.json exists with all tools backfilled so the
    # agent can read/edit it and the approval store resolves from it.
    from backend.app.agent.approval import get_approval_store

    get_approval_store().ensure_complete(user.id)

    # Shared mutable set so the list_capabilities tool closure and the
    # agent loop both see the same activation state.  This prevents the
    # tool from returning the full SKILL.md instructions a second time
    # when the LLM redundantly calls list_capabilities for a category
    # that was already activated in a prior round.
    activated_specialists: set[str] = set()

    agent = ClawboltAgent(
        user=user,
        channel=channel,
        publish_outbound=publish_outbound,
        chat_id=to_address,
        tool_context=tool_context,
        registry=default_registry,
        session_id=session_id,
        excluded_tool_names=disabled_sub_tools or None,
        request_id=request_id,
        activated_specialists=activated_specialists,
    )

    # Start with core tools only; specialist tools are discovered on demand
    # via the list_capabilities meta-tool. Exclude user-disabled groups and
    # individual sub-tools.
    tools = await default_registry.create_core_tools(
        tool_context,
        excluded_factories=disabled_groups or None,
        excluded_tool_names=disabled_sub_tools or None,
    )
    specialist_summaries = default_registry.get_available_specialist_summaries(
        tool_context, excluded_factories=disabled_groups or None
    )
    unauthenticated = default_registry.get_unauthenticated_specialists(
        tool_context, excluded_factories=disabled_groups or None
    )
    disabled_specialist_subs = default_registry.get_disabled_specialist_sub_tools(
        disabled_sub_tools or set()
    )
    if specialist_summaries or unauthenticated:
        tools.append(
            create_list_capabilities_tool(
                specialist_summaries,
                unauthenticated=unauthenticated,
                disabled_sub_tools=disabled_specialist_subs or None,
                activated_specialists=activated_specialists,
            )
        )
    agent.register_tools(tools)

    # Build onboarding prompt now that tools are available, so that tool
    # guidelines (e.g. "reply directly with text") are included.
    if is_onboarding and not system_prompt_override:
        system_prompt_override = build_onboarding_system_prompt(user, tools=tools)

    # During onboarding the agent must delete BOOTSTRAP.md and send
    # replies without prompting.  Pre-approve these tools so the
    # approval gate doesn't block the bootstrap flow.  (write_file and
    # edit_file already default to ALWAYS.)
    if is_onboarding:
        from backend.app.agent.approval import PermissionLevel, get_approval_store
        from backend.app.agent.tools.names import ToolName

        _onboarding_auto_tools = (
            ToolName.DELETE_FILE,
            ToolName.SEND_REPLY,
            ToolName.SEND_MEDIA_REPLY,
        )
        store = get_approval_store()
        for tool_name in _onboarding_auto_tools:
            store.set_permission(user.id, tool_name, PermissionLevel.ALWAYS)

    logger.debug(
        "Agent initialized for user %s, message seq=%d with %d core tools, "
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
            "Content filter blocked message seq %d for user %s",
            message.seq,
            user.id,
        )
        return AgentResponse(reply_text=CONTENT_FILTER_FALLBACK, is_error_fallback=True)
    except AuthenticationError:
        logger.critical(
            "LLM authentication failed processing message seq %d for user %s",
            message.seq,
            user.id,
        )
        return AgentResponse(reply_text=AUTH_ERROR_FALLBACK, is_error_fallback=True)
    except Exception:
        logger.exception(
            "Agent processing failed for message seq %d, user %s",
            message.seq,
            user.id,
        )
        return AgentResponse(reply_text=AGENT_ERROR_FALLBACK, is_error_fallback=True)


async def persist_outbound(
    session: SessionState,
    user_id: str,
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
    """Download media and initialize storage backend.

    Preserves any already-downloaded media on the context (e.g. webchat
    file uploads) and merges them with newly downloaded media from
    ``media_urls`` (e.g. Telegram file-id references).
    """
    pre_downloaded = list(ctx.downloaded_media)
    newly_downloaded, ctx.storage = await prepare_media(
        ctx.user, ctx.message, ctx.media_urls, download_media=ctx.download_media
    )
    ctx.downloaded_media = pre_downloaded + newly_downloaded

    # Auto-save pre-downloaded media (webchat uploads) to storage
    if ctx.storage and pre_downloaded:
        try:
            await auto_save_media(ctx.user, ctx.storage, pre_downloaded)
        except Exception:
            logger.debug("Auto-save pre-downloaded media failed, continuing")

    return ctx


async def build_context_step(ctx: PipelineContext) -> PipelineContext:
    """Run media pipeline and build combined context."""
    ctx.combined_context = await build_message_context(
        ctx.session, ctx.message, ctx.user, ctx.media_urls, ctx.downloaded_media
    )
    return ctx


async def load_history_step(ctx: PipelineContext) -> PipelineContext:
    """Load conversation history and set up onboarding."""
    ctx.conversation_history = await load_conversation_history(ctx.session)
    ctx.is_onboarding = is_onboarding_needed(ctx.user)
    onboarding_sub = OnboardingSubscriber(ctx.user, ctx.is_onboarding)
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
        to_address=ctx.to_address,
        downloaded_media=ctx.downloaded_media,
        channel=ctx.channel,
        system_prompt_override=ctx.system_prompt_override,
        is_onboarding=ctx.is_onboarding,
        event_subscribers=ctx.event_subscribers,
        session_id=ctx.session.session_id,
        request_id=ctx.request_id,
    )
    return ctx


async def persist_system_prompt_step(ctx: PipelineContext) -> PipelineContext:
    """Save the system prompt to the session on first message."""
    if ctx.response and ctx.response.system_prompt:
        session_store = get_session_store(ctx.user.id)
        await session_store.update_initial_system_prompt(ctx.session, ctx.response.system_prompt)
    return ctx


async def finalize_onboarding_step(ctx: PipelineContext) -> PipelineContext:
    """Append onboarding completion note if applicable."""
    if ctx._onboarding_sub and ctx.response:
        ctx._onboarding_sub.finalize(ctx.response)
    return ctx


async def dispatch_reply_step(ctx: PipelineContext) -> PipelineContext:
    """Send reply via the message bus.

    Messages are dispatched via the bus so the outbound dispatcher can route
    them to the correct channel or resolve a web chat SSE future.
    """
    if ctx.response and ctx.channel:
        from backend.app.bus import OutboundMessage, message_bus

        sent_reply = any(
            ToolTags.SENDS_REPLY in tc.tags and not tc.is_error for tc in ctx.response.tool_calls
        )
        if not sent_reply and ctx.response.reply_text:
            outbound = OutboundMessage(
                channel=ctx.channel,
                chat_id=ctx.to_address,
                content=ctx.response.reply_text,
                request_id=ctx.request_id,
            )
            await message_bus.publish_outbound(outbound)
        elif not sent_reply and not ctx.response.reply_text and ctx.request_id:
            # Resolve SSE future with empty content so webchat clients
            # know processing is complete without rendering a message.
            outbound = OutboundMessage(
                channel=ctx.channel,
                chat_id=ctx.to_address,
                content="",
                request_id=ctx.request_id,
            )
            await message_bus.publish_outbound(outbound)
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
    persist_system_prompt_step,
    finalize_onboarding_step,
    dispatch_reply_step,
    persist_outbound_step,
]

# Premium (or other plugins) can register a custom pipeline via
# ``set_pipeline_override()`` to inject extra steps (e.g. quota checks).
_pipeline_override: list[PipelineStep] | None = None


def set_pipeline_override(pipeline: list[PipelineStep]) -> None:
    """Register a custom pipeline that replaces ``DEFAULT_PIPELINE``.

    Called by the premium plugin at import time to inject quota-check
    and usage-tracking steps into the agent pipeline.
    """
    global _pipeline_override
    _pipeline_override = pipeline


def get_active_pipeline() -> list[PipelineStep]:
    """Return the currently active pipeline (override or default)."""
    return _pipeline_override if _pipeline_override is not None else DEFAULT_PIPELINE


def build_pipeline(
    *,
    replace: dict[PipelineStep, PipelineStep] | None = None,
    insert_before: dict[PipelineStep, list[PipelineStep]] | None = None,
    insert_after: dict[PipelineStep, list[PipelineStep]] | None = None,
) -> list[PipelineStep]:
    """Build a pipeline by modifying ``DEFAULT_PIPELINE``.

    This allows plugins (e.g. premium) to define their pipeline relative
    to the OSS default instead of hardcoding the full list.

    * ``replace``: swap one step for another
    * ``insert_before``: inject steps immediately before a given step
    * ``insert_after``: inject steps immediately after a given step
    """
    replace = replace or {}
    insert_before = insert_before or {}
    insert_after = insert_after or {}

    result: list[PipelineStep] = []
    for step in DEFAULT_PIPELINE:
        if step in insert_before:
            result.extend(insert_before[step])
        result.append(replace.get(step, step))
        if step in insert_after:
            result.extend(insert_after[step])
    return result


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def _create_sse_event_forwarder(
    request_id: str,
) -> Callable[[AgentEvent], Awaitable[None]]:
    """Create an event subscriber that forwards tool events to the per-request SSE stream."""

    async def _forward(event: AgentEvent) -> None:
        if isinstance(event, ToolExecutionStartEvent):
            from backend.app.bus import message_bus

            await message_bus.publish_event(
                request_id,
                {"type": "tool_call", "tool_name": event.tool_name},
            )

    return _forward


def _create_activity_forwarder(
    user_id: str,
    channel: str,
) -> Callable[[AgentEvent], Awaitable[None]]:
    """Create an event subscriber that forwards agent events to the user activity stream.

    Publishes to all connected dashboard clients for this user, regardless
    of which channel (Telegram, webchat, etc.) originated the message.
    """

    async def _forward(event: AgentEvent) -> None:
        from backend.app.bus import message_bus

        if isinstance(event, TurnStartEvent):
            await message_bus.publish_activity(user_id, {"type": "thinking", "channel": channel})
        elif isinstance(event, ToolExecutionStartEvent):
            await message_bus.publish_activity(
                user_id,
                {"type": "tool_call", "tool_name": event.tool_name, "channel": channel},
            )
        elif isinstance(event, AgentEndEvent):
            await message_bus.publish_activity(user_id, {"type": "done", "channel": channel})

    return _forward


async def handle_inbound_message(
    user: User,
    session: SessionState,
    message: StoredMessage,
    media_urls: list[tuple[str, str]],
    pipeline: list[PipelineStep] | None = None,
    downloaded_media: list[DownloadedMedia] | None = None,
    channel: str = "",
    request_id: str = "",
    download_media: Callable[[str], Awaitable[DownloadedMedia]] | None = None,
) -> AgentResponse:
    """Full message processing pipeline.

    Orchestrates discrete pipeline steps via a composable list of
    ``PipelineStep`` callables. Pass a custom ``pipeline`` to add,
    remove, or reorder steps; defaults to the active pipeline (which
    may be overridden by a premium plugin).
    """
    logger.debug(
        "Handling inbound message seq=%d for user %s, %d media attachment(s)",
        message.seq,
        user.id,
        len(media_urls),
    )
    db = SessionLocal()
    try:
        route = db.query(ChannelRoute).filter_by(user_id=user.id, channel=channel).first()
        to_address = (
            (route.channel_identifier if route else None) or user.channel_identifier or user.phone
        )
    finally:
        db.close()
    if not to_address:
        logger.error(
            "User %s has no channel_identifier or phone -- cannot send replies",
            user.id,
        )
        return AgentResponse(reply_text="")

    ctx = PipelineContext(
        user=user,
        session=session,
        message=message,
        media_urls=media_urls,
        channel=channel,
        to_address=to_address,
        downloaded_media=downloaded_media or [],
        download_media=download_media,
        request_id=request_id,
    )
    # Stream tool execution events to the web chat SSE endpoint
    if request_id:
        ctx.event_subscribers.append(_create_sse_event_forwarder(request_id))
    # Stream agent activity to all connected dashboard clients
    ctx.event_subscribers.append(_create_activity_forwarder(str(user.id), channel))
    ctx = await run_pipeline(ctx, pipeline or get_active_pipeline())
    return ctx.response or AgentResponse(reply_text="")
