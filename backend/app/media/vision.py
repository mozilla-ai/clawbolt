import base64
import logging
from typing import Any, cast

from any_llm import amessages
from any_llm.types.messages import MessageResponse

from backend.app.agent.llm_parsing import get_response_text
from backend.app.config import settings

logger = logging.getLogger(__name__)

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
    b64_image = base64.b64encode(image_bytes).decode("utf-8")
    user_content = _build_vision_content(b64_image, mime_type, context)

    model = settings.vision_model or settings.llm_model
    logger.info("Using vision model: %s (provider=%s)", model, settings.llm_provider)

    response = cast(
        MessageResponse,
        await amessages(
            model=model,
            provider=settings.llm_provider,
            api_base=settings.llm_api_base,
            system=VISION_SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": user_content},
            ],
            max_tokens=settings.llm_max_tokens_vision,
        ),
    )
    logger.debug("Vision LLM response received for mime_type=%s", mime_type)
    return get_response_text(response)
