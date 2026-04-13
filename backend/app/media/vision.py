import base64
import io
import logging
from typing import Any, cast

from any_llm import amessages
from any_llm.types.messages import MessageResponse
from PIL import Image

from backend.app.agent.llm_parsing import get_response_text
from backend.app.config import settings
from backend.app.services.llm_service import (
    prepare_system_with_caching,
    reasoning_effort_to_thinking,
)

logger = logging.getLogger(__name__)

# Anthropic API limit: base64-encoded image must be under 5 MB.
# base64 inflates size by ~4/3, so raw bytes must stay under ~3.75 MB.
# Use a conservative raw-byte target to leave headroom.
_MAX_BASE64_BYTES = 5_242_880
_MAX_RAW_BYTES = (_MAX_BASE64_BYTES * 3) // 4  # ~3,932,160

_JPEG_QUALITY_STEPS = [85, 70, 50, 30]
_RESIZE_SCALES = [0.75, 0.5, 0.35]


def compress_image_for_api(image_bytes: bytes, mime_type: str) -> tuple[bytes, str]:
    """Compress an image so its base64 encoding stays under the API size limit.

    Returns the (possibly compressed) bytes and the output MIME type.
    If the image already fits, the original bytes and MIME type are returned unchanged.
    """
    if len(image_bytes) <= _MAX_RAW_BYTES:
        return image_bytes, mime_type

    logger.info(
        "Image too large for API (%d bytes, limit %d). Compressing.",
        len(image_bytes),
        _MAX_RAW_BYTES,
    )

    img = Image.open(io.BytesIO(image_bytes))
    if img.mode in ("RGBA", "P", "LA"):
        img = img.convert("RGB")

    # Try progressively lower JPEG quality at the original resolution.
    for quality in _JPEG_QUALITY_STEPS:
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        if buf.tell() <= _MAX_RAW_BYTES:
            logger.info("Compressed to %d bytes at quality=%d", buf.tell(), quality)
            return buf.getvalue(), "image/jpeg"

    # If quality reduction alone is insufficient, also scale down.
    for scale in _RESIZE_SCALES:
        new_size = (int(img.width * scale), int(img.height * scale))
        resized = img.resize(new_size, Image.Resampling.LANCZOS)
        for quality in _JPEG_QUALITY_STEPS:
            buf = io.BytesIO()
            resized.save(buf, format="JPEG", quality=quality, optimize=True)
            if buf.tell() <= _MAX_RAW_BYTES:
                logger.info(
                    "Compressed to %d bytes at scale=%.2f, quality=%d",
                    buf.tell(),
                    scale,
                    quality,
                )
                return buf.getvalue(), "image/jpeg"

    # Last resort: return the smallest version we produced.
    logger.warning("Could not compress image below %d bytes; using smallest result", _MAX_RAW_BYTES)
    return buf.getvalue(), "image/jpeg"


VISION_SYSTEM_PROMPT = (
    "You are analyzing an image sent by a user. "
    "Describe what you see in detail, focusing on: "
    "materials, dimensions, condition, damage, work needed, safety concerns. "
    "Be specific and technical."
)


def _build_vision_content(
    b64_image: str, mime_type: str, context: str = ""
) -> list[dict[str, Any]]:
    """Build the content block list for a vision LLM request."""
    blocks: list[dict[str, Any]] = []
    if context:
        blocks.append({"type": "text", "text": context})
    blocks.append(
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": mime_type,
                "data": b64_image,
            },
        }
    )
    return blocks


async def analyze_image(image_bytes: bytes, mime_type: str, context: str = "") -> str:
    """Send an image to a vision LLM and get a text description."""
    logger.info(
        "Sending image to vision LLM: mime_type=%s, size=%d bytes", mime_type, len(image_bytes)
    )
    image_bytes, mime_type = compress_image_for_api(image_bytes, mime_type)
    b64_image = base64.b64encode(image_bytes).decode("utf-8")
    user_content = _build_vision_content(b64_image, mime_type, context)

    model = settings.vision_model or settings.llm_model
    provider = settings.vision_provider or settings.llm_provider
    logger.info("Using vision model: %s (provider=%s)", model, provider)

    response = cast(
        MessageResponse,
        await amessages(
            model=model,
            provider=provider,
            api_base=settings.llm_api_base,
            system=prepare_system_with_caching(VISION_SYSTEM_PROMPT),
            messages=[
                {"role": "user", "content": user_content},
            ],
            max_tokens=settings.llm_max_tokens_vision,
            thinking=reasoning_effort_to_thinking(settings.reasoning_effort),
        ),
    )
    logger.debug("Vision LLM response received for mime_type=%s", mime_type)
    return get_response_text(response)
