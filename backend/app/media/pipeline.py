import asyncio
import logging
from dataclasses import dataclass

from backend.app.agent import media_staging
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

    Invoked by the ``analyze_photo`` tool when the agent decides vision is
    worth running on a staged photo. Returns the analysis text; falls back
    to :data:`VISION_FALLBACK` on error so callers always get a non-empty
    string.
    """
    try:
        return await analyze_image(content, mime_type, context=text_body)
    except Exception:
        logger.exception("Vision analysis failed (mime_type=%s)", mime_type)
        return VISION_FALLBACK


async def _process_single_media(
    media: DownloadedMedia,
    handle: str | None = None,
) -> ProcessedMedia:
    """Classify a staged media item for the combined context.

    Vision is NOT run here. The agent invokes ``analyze_photo(handle)`` when
    it decides a photo description is worth the call.
    """
    category = classify_media(media.mime_type)
    logger.debug("Media classified: %s -> %s", media.mime_type, category)

    if category == "image":
        extracted_text = ""
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
    """Classify all media in a message and build the agent's combined context.

    Vision is deferred to the agent (``analyze_photo`` tool) on every inbound
    message. Each media item gets classified and paired with its staging
    handle so the combined context surfaces what the agent can reference.
    """
    logger.info("Processing %d media item(s)", len(media_items))

    if media_items and not user_id:
        logger.warning(
            "process_message_media: user_id is missing; %d media item(s) "
            "will not be surfaced to the agent via a handle",
            len(media_items),
        )

    handles: list[str | None] = [
        media_staging.get_handle_for(user_id, m.original_url) if user_id else None
        for m in media_items
    ]

    tasks = [_process_single_media(m, handle=handles[i]) for i, m in enumerate(media_items)]
    media_results = list(await asyncio.gather(*tasks))
    logger.info(
        "Media processing complete: %s",
        ", ".join(f"{r.category} ({len(r.extracted_text)} chars)" for r in media_results),
    )

    parts: list[str] = []
    if text_body:
        parts.append(f"[Text message]: {text_body!r}")
    for i, result in enumerate(media_results):
        label = _format_label(result.category, i + 1, result.handle)
        if result.extracted_text:
            parts.append(f"[{label}]: {result.extracted_text}")
        elif result.category == "image" and result.handle:
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
