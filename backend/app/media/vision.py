import base64

from any_llm import acompletion

from backend.app.config import settings

VISION_SYSTEM_PROMPT = (
    "You are analyzing an image sent by a contractor. "
    "Describe what you see in detail, focusing on: "
    "materials, dimensions, condition, damage, work needed, safety concerns. "
    "Be specific and technical."
)


async def analyze_image(image_bytes: bytes, mime_type: str, context: str = "") -> str:
    """Send an image to a vision LLM and get a text description."""
    b64_image = base64.b64encode(image_bytes).decode("utf-8")
    data_url = f"data:{mime_type};base64,{b64_image}"

    user_content: list[dict[str, object]] = [
        {"type": "image_url", "image_url": {"url": data_url}},
    ]
    if context:
        user_content.insert(0, {"type": "text", "text": context})

    response = await acompletion(
        model=settings.vision_model,
        provider=settings.llm_provider,
        messages=[
            {"role": "system", "content": VISION_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        max_tokens=1000,
    )
    return response.choices[0].message.content
