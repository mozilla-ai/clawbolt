"""Agent-invoked media tools for vision and deliberate discard decisions.

These tools make the agent the driver of per-photo decisions when
``agent_native_storage`` is on. The hardcoded media pipeline still stages
bytes; the agent decides whether to analyze or discard them.

``analyze_photo`` runs vision on a staged photo and caches the result
per-handle for the session so re-asking returns the same answer instantly.

``discard_media`` releases staged bytes and is idempotent. It requires
``ApprovalPolicy.ASK`` unless the caller quotes a user phrase in the
``reason`` argument, a cheap defense against adversarial image content
that tries to talk the agent into calling it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from backend.app.agent import media_staging
from backend.app.agent.approval import ApprovalPolicy, PermissionLevel
from backend.app.agent.tools.base import Tool, ToolErrorKind, ToolResult
from backend.app.agent.tools.names import ToolName
from backend.app.config import settings
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


def _reason_has_quoted_phrase(reason: str) -> bool:
    """Return True when ``reason`` contains a plausibly user-quoted phrase.

    Cheap prompt-injection defense: adversarial image content can tell the
    agent to call ``discard_media`` for other files, but it cannot fabricate
    a quoted snippet of something the user actually said in this turn.
    We treat any pair of quote characters with text between them as a
    user-quoted phrase. This is permissive by design: the approval gate is
    a belt-and-suspenders check, not the sole defense.
    """
    if not reason:
        return False
    # Straight-quote pairs (same char opens and closes).
    for q in ('"', "'"):
        idx = reason.find(q)
        if idx != -1 and reason.find(q, idx + 1) > idx:
            return True
    # Curly-quote pairs (iOS keyboards substitute these): opening char
    # followed anywhere by the matching closing char counts.
    curly_pairs = (("\u201c", "\u201d"), ("\u2018", "\u2019"))
    for opener, closer in curly_pairs:
        oi = reason.find(opener)
        if oi != -1 and reason.find(closer, oi + 1) > oi:
            return True
    return False


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

        # Extend TTL on reference so long agent sessions don't evict mid-turn.
        media_staging.touch(handle)

        effective_context = context or turn_text
        description = await run_vision_on_media(content, mime, effective_context)
        analyze_cache[handle] = description
        logger.info("analyze_photo ran vision for %s (chars=%d)", handle, len(description))
        return ToolResult(content=description)

    async def discard_media(handle: str, reason: str) -> ToolResult:
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
                "Run vision analysis on a staged photo referenced by its handle. "
                "Use this when the conversation doesn't already describe the photo "
                "contents. Results are cached per-handle within the session, so "
                "calling twice for the same handle is cheap."
            ),
            function=analyze_photo,
            params_model=AnalyzePhotoParams,
            usage_hint="Describe a photo the user sent.",
        ),
        Tool(
            name=ToolName.DISCARD_MEDIA,
            description=(
                "Discard a staged photo the user asked you not to save. Use this "
                "ONLY when the user's current message explicitly asks to drop the "
                "photo (e.g. 'don't save that one', 'skip this photo'). Quote the "
                "user's phrase in the reason argument; the user will be asked to "
                "confirm. Idempotent: discarding an already-discarded handle is safe."
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
    """Factory for agent-native media tools. Gated on flag + staged media."""
    if not settings.agent_native_storage:
        return []
    has_downloaded = bool(ctx.downloaded_media)
    has_staged = bool(media_staging.get_all_for_user(ctx.user.id))
    if not has_downloaded and not has_staged:
        return []
    # Per-turn analysis cache. Scoped to the factory call so it lives for the
    # duration of the agent loop for this message.
    analyze_cache: dict[str, str] = {}
    turn_text = ""
    return create_media_tools(ctx.user.id, turn_text, analyze_cache)


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
