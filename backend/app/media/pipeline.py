import asyncio
import logging
from dataclasses import dataclass

from backend.app.agent import media_staging
from backend.app.config import settings
from backend.app.media.download import DownloadedMedia, classify_media
from backend.app.media.vision import analyze_image

logger = logging.getLogger(__name__)

# Fallback messages when media processing is unavailable
VISION_FALLBACK = "[Photo - vision analysis not available]"

# Media type display labels used in combined context output
MEDIA_TYPE_LABELS: dict[str, str] = {
    "image": "Photo",
    "pdf": "Document",
}


@dataclass
class ProcessedMedia:
    original_url: str
    mime_type: str
    category: str
    extracted_text: str
    handle: str | None = None


@dataclass
class PipelineResult:
    text_body: str
    media_results: list[ProcessedMedia]
    combined_context: str


async def run_vision_on_media(content: bytes, mime_type: str, text_body: str = "") -> str:
    """Run vision analysis on media bytes with optional caption context.

    Extracted from the pipeline so both the flag-off auto-vision path and the
    agent-invoked ``analyze_photo`` tool share one code path. Returns the
    analysis text; falls back to :data:`VISION_FALLBACK` on error so callers
    always get a non-empty string.
    """
    try:
        return await analyze_image(content, mime_type, context=text_body)
    except Exception:
        logger.exception("Vision analysis failed (mime_type=%s)", mime_type)
        return VISION_FALLBACK


async def _process_single_media(
    media: DownloadedMedia,
    index: int,
    context: str = "",
    skip_vision: bool = False,
    handle: str | None = None,
) -> ProcessedMedia:
    """Process a single media item based on its type."""
    category = classify_media(media.mime_type)
    logger.debug("Media classified: %s → %s", media.mime_type, category)
    extracted_text = ""

    if category == "image":
        if skip_vision:
            # Agent-native mode: defer vision to the analyze_photo tool so the
            # agent decides per-photo whether analysis is worth the call.
            extracted_text = ""
        else:
            extracted_text = await run_vision_on_media(media.content, media.mime_type, context)
    else:
        logger.info("Skipping unsupported media type: %s", media.mime_type)
        extracted_text = f"[{category.title()} file - processing not available]"

    return ProcessedMedia(
        original_url=media.original_url,
        mime_type=media.mime_type,
        category=category,
        extracted_text=extracted_text,
        handle=handle,
    )


async def process_message_media(
    text_body: str,
    media_items: list[DownloadedMedia],
    user_id: str | None = None,
) -> PipelineResult:
    """Process all media in a message and combine into unified context.

    When :attr:`settings.agent_native_storage` is on, vision is deferred to the
    agent (``analyze_photo`` tool). Each media item still gets classified and
    staged, but ``extracted_text`` is empty and the combined context surfaces
    the staging handle so the agent knows what to reference.
    """
    logger.info("Processing %d media item(s)", len(media_items))
    skip_vision = bool(settings.agent_native_storage)

    handles: list[str | None] = []
    for m in media_items:
        handle = (
            media_staging.get_handle_for(user_id, m.original_url)
            if user_id and skip_vision
            else None
        )
        handles.append(handle)

    tasks = [
        _process_single_media(m, i, context=text_body, skip_vision=skip_vision, handle=handles[i])
        for i, m in enumerate(media_items)
    ]
    media_results = await asyncio.gather(*tasks)
    media_results = list(media_results)
    logger.info(
        "Media processing complete: %s",
        ", ".join(f"{r.category} ({len(r.extracted_text)} chars)" for r in media_results),
    )

    # Build combined context
    parts: list[str] = []
    if text_body:
        parts.append(f"[Text message]: {text_body!r}")
    for i, result in enumerate(media_results):
        label = _format_label(result.category, i + 1, result.handle)
        if result.extracted_text:
            parts.append(f"[{label}]: {result.extracted_text}")
        elif skip_vision and result.category == "image" and result.handle:
            # Agent-native mode: surface the handle so the agent can call
            # analyze_photo(handle) if it decides vision is needed.
            parts.append(
                f"[{label}]: (staged, call analyze_photo(handle={result.handle!r})"
                " if you need a description)"
            )

    combined_context = "\n\n".join(parts)

    return PipelineResult(
        text_body=text_body,
        media_results=media_results,
        combined_context=combined_context,
    )


def _format_label(category: str, index: int, handle: str | None = None) -> str:
    """Format a label for a media item in the combined context."""
    label = MEDIA_TYPE_LABELS.get(category, "Attachment")
    base = f"{label} {index}"
    if handle:
        base += f", handle={handle}"
    return base
