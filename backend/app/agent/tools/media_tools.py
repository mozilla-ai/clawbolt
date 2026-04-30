"""Agent-invoked media tools for vision and deliberate discard decisions.

The media pipeline stages inbound bytes; the agent decides per-photo whether
to analyze, save, route, or discard via tool calls.

``analyze_photo`` runs vision on a staged photo and caches the result
per-handle for the session so re-asking returns the same answer instantly.

``discard_media`` releases staged bytes and is idempotent. Always gated by
``ApprovalPolicy.ASK`` so the user confirms before the bytes are dropped;
the tool description instructs the agent to only call it when the current
turn's text explicitly asks to skip saving.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from backend.app.agent import media_staging
from backend.app.agent.approval import ApprovalPolicy, PermissionLevel
from backend.app.agent.tools.base import Tool, ToolErrorKind, ToolResult
from backend.app.agent.tools.names import ToolName
from backend.app.media.pipeline import run_vision_on_media

if TYPE_CHECKING:
    from backend.app.agent.tools.registry import ToolContext

logger = logging.getLogger(__name__)


class AnalyzePhotoParams(BaseModel):
    """Parameters for the analyze_photo tool."""

    handle: str = Field(
        description="The media handle token (e.g. 'media_ab12cd') from the attachment label.",
    )
    context: str = Field(
        default="",
        description=(
            "Optional short context to guide the analysis. "
            "Leave empty to use the current turn's message text."
        ),
    )


class DiscardMediaParams(BaseModel):
    """Parameters for the discard_media tool."""

    handle: str = Field(
        description="The media handle token to discard.",
    )
    reason: str = Field(
        description=(
            "Why the media is being discarded. Quote the user's exact "
            "request (e.g. 'user said \"don\\'t save this one\"') to skip "
            "the approval prompt; otherwise the user is asked first."
        ),
    )


def create_media_tools(
    user_id: str,
    turn_text: str,
    analyze_cache: dict[str, str],
) -> list[Tool]:
    """Build the agent-native media tool set bound to this turn's context."""

    async def analyze_photo(handle: str, context: str = "") -> ToolResult:
        cached = analyze_cache.get(handle)
        if cached is not None:
            return ToolResult(content=cached)

        entry = media_staging.get_by_handle(handle)
        if entry is None:
            return ToolResult(
                content=(
                    f"No staged media found for handle {handle!r}. "
                    "It may have expired or already been discarded."
                ),
                is_error=True,
                error_kind=ToolErrorKind.NOT_FOUND,
            )

        stored_user_id, _original_url, content, mime = entry
        if stored_user_id != user_id:
            return ToolResult(
                content=f"Handle {handle!r} does not belong to the current user.",
                is_error=True,
                error_kind=ToolErrorKind.PERMISSION,
            )

        # Vision only supports images. PDFs and other documents would crash
        # the image compressor or confuse the vision LLM.
        if not mime.startswith("image/"):
            return ToolResult(
                content=(
                    f"Handle {handle!r} is {mime}, not an image. "
                    "analyze_photo only works on photos."
                ),
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
            )

        # Extend TTL on reference so long agent sessions don't evict mid-turn.
        media_staging.touch(handle)

        effective_context = context or turn_text
        description = await run_vision_on_media(content, mime, effective_context)
        analyze_cache[handle] = description
        logger.info("analyze_photo ran vision for %s (chars=%d)", handle, len(description))
        return ToolResult(content=description)

    async def discard_media(handle: str, reason: str) -> ToolResult:
        # Defense in depth: same cross-user ownership check as analyze_photo.
        # Handles are unguessable in practice but we still scope every
        # destructive operation to the current user.
        entry = media_staging.get_by_handle(handle)
        if entry is not None and entry[0] != user_id:
            return ToolResult(
                content=f"Handle {handle!r} does not belong to the current user.",
                is_error=True,
                error_kind=ToolErrorKind.PERMISSION,
            )

        removed = media_staging.evict_by_handle(handle)
        if not removed:
            # Idempotent: a second call (or a call after expiry) reports
            # success so the agent does not get stuck retrying.
            return ToolResult(
                content=f"Media {handle!r} is not staged (already discarded or expired)."
            )
        analyze_cache.pop(handle, None)
        logger.info("discard_media evicted %s (reason=%r)", handle, reason)
        return ToolResult(content=f"Discarded {handle} (reason: {reason})")

    return [
        Tool(
            name=ToolName.ANALYZE_PHOTO,
            description=(
                "Run vision analysis on a staged photo. Default: do not call. "
                "Use only when the user has asked you to look at the image, or "
                "you have a clear need to see its contents to help. Results "
                "are cached per-handle within the session so calling twice on "
                "the same handle is cheap."
            ),
            function=analyze_photo,
            params_model=AnalyzePhotoParams,
            usage_hint=(
                "analyze_photo describes a photo. Call only when the user "
                "asked for analysis or you genuinely need to see what's in "
                "the image to help."
            ),
        ),
        Tool(
            name=ToolName.DISCARD_MEDIA,
            description=(
                "Discard a staged photo the user asked you not to save. Use this "
                "only when the user's current message explicitly asks to drop the "
                "photo. Quote the user's phrase in the reason argument; the user "
                "will be asked to confirm. Idempotent: discarding an already-"
                "discarded handle is safe."
            ),
            function=discard_media,
            params_model=DiscardMediaParams,
            usage_hint="Drop a staged photo per explicit user request.",
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ASK,
                description_builder=lambda args: (
                    f"Discard staged media {args.get('handle', '?')}"
                    f" ({args.get('reason', 'no reason given')})"
                ),
            ),
        ),
    ]


def _media_factory(ctx: ToolContext) -> list[Tool]:
    """Factory for agent-native media tools. Gated on presence of staged media."""
    has_downloaded = bool(ctx.downloaded_media)
    has_staged = bool(media_staging.get_all_for_user(ctx.user.id))
    if not has_downloaded and not has_staged:
        return []
    # Per-turn analysis cache. Scoped to the factory call so it lives for the
    # duration of the agent loop for this message.
    analyze_cache: dict[str, str] = {}
    return create_media_tools(ctx.user.id, ctx.turn_text, analyze_cache)


def _register() -> None:
    from backend.app.agent.tools.registry import SubToolInfo, default_registry

    default_registry.register(
        "media",
        _media_factory,
        core=True,
        summary="Describe and discard staged photos (agent-native storage)",
        sub_tools=[
            SubToolInfo(
                ToolName.ANALYZE_PHOTO,
                "Run vision analysis on a staged photo",
                default_permission="always",
            ),
            SubToolInfo(
                ToolName.DISCARD_MEDIA,
                "Discard a staged photo per user request",
                default_permission="ask",
            ),
        ],
    )


_register()
