import asyncio
import logging
from dataclasses import dataclass

from backend.app.media.audio import transcribe_audio
from backend.app.media.download import DownloadedMedia, classify_media
from backend.app.media.vision import analyze_image

logger = logging.getLogger(__name__)

# Fallback messages when media processing is unavailable
VISION_FALLBACK = "[Photo — vision analysis not available]"
VIDEO_ERROR_FALLBACK = "[Video file - transcription not available]"

# Media type display labels used in combined context output
MEDIA_TYPE_LABELS: dict[str, str] = {
    "image": "Photo",
    "audio": "Voice note",
    "video": "Video",
    "pdf": "Document",
}


@dataclass
class ProcessedMedia:
    original_url: str
    mime_type: str
    category: str
    extracted_text: str


@dataclass
class PipelineResult:
    text_body: str
    media_results: list[ProcessedMedia]
    combined_context: str


async def _process_single_media(
    media: DownloadedMedia, index: int, context: str = ""
) -> ProcessedMedia:
    """Process a single media item based on its type."""
    category = classify_media(media.mime_type)
    logger.debug("Media classified: %s → %s", media.mime_type, category)
    extracted_text = ""

    if category == "image":
        try:
            extracted_text = await analyze_image(media.content, media.mime_type, context=context)
        except Exception:
            logger.exception(
                "Vision analysis failed for %s (mime_type=%s)", media.original_url, media.mime_type
            )
            extracted_text = VISION_FALLBACK
    elif category == "audio":
        extracted_text = await transcribe_audio(media.content, media.mime_type)
    elif category == "video":
        # Future: extract audio track. For now, try audio transcription.
        try:
            extracted_text = await transcribe_audio(media.content, media.mime_type)
        except Exception:
            logger.warning("Could not process video file: %s", media.original_url)
            extracted_text = VIDEO_ERROR_FALLBACK
    else:
        logger.info("Skipping unsupported media type: %s", media.mime_type)
        extracted_text = f"[{category.title()} file - processing not available]"

    return ProcessedMedia(
        original_url=media.original_url,
        mime_type=media.mime_type,
        category=category,
        extracted_text=extracted_text,
    )


async def process_message_media(
    text_body: str,
    media_items: list[DownloadedMedia],
) -> PipelineResult:
    """Process all media in a message and combine into unified context."""
    logger.info("Processing %d media item(s)", len(media_items))
    tasks = [_process_single_media(m, i, context=text_body) for i, m in enumerate(media_items)]
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
        label = _format_label(result.category, i + 1)
        if result.extracted_text:
            parts.append(f"[{label}]: {result.extracted_text}")

    combined_context = "\n\n".join(parts)

    return PipelineResult(
        text_body=text_body,
        media_results=media_results,
        combined_context=combined_context,
    )


def _format_label(category: str, index: int) -> str:
    """Format a label for a media item in the combined context."""
    label = MEDIA_TYPE_LABELS.get(category, "Attachment")
    if label == "Voice note":
        return label
    return f"{label} {index}"
