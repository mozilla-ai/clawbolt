"""Session compaction: extract durable facts from aging messages.

When conversation history reaches the configured limit, messages about to be
trimmed are passed through a lightweight LLM call that extracts key facts.
Those facts are persisted via the existing memory subsystem so they survive
after the messages leave the context window.
"""

import json
import logging
from typing import Any, cast

from any_llm import acompletion
from any_llm.types.completion import ChatCompletion
from sqlalchemy.orm import Session

from backend.app.agent.memory import save_memory
from backend.app.agent.messages import AgentMessage, AssistantMessage, UserMessage
from backend.app.config import settings

logger = logging.getLogger(__name__)

COMPACTION_SYSTEM_PROMPT = (
    "You are a fact-extraction assistant. You will receive a block of conversation "
    "messages between a contractor and their AI assistant. Extract durable facts worth "
    "remembering long-term, such as:\n"
    "- Client names, phone numbers, addresses\n"
    "- Pricing decisions or quoted rates\n"
    "- Material preferences or supplier names\n"
    "- Job details, measurements, or scheduling commitments\n"
    "- Business preferences or policies\n\n"
    "Return a JSON array of objects, each with:\n"
    '  {"key": "<short_snake_case_identifier>", "value": "<fact>", "category": "<category>"}\n\n'
    "Valid categories: pricing, client, job, supplier, scheduling, general\n\n"
    "Rules:\n"
    "- Only extract facts that would be useful in future conversations.\n"
    "- Skip greetings, small talk, and transient information.\n"
    "- If there are no durable facts, return an empty array: []\n"
    "- Return ONLY the JSON array, no other text."
)


def _format_messages_for_compaction(messages: list[AgentMessage]) -> str:
    """Format a list of agent messages into a readable text block for the LLM."""
    lines: list[str] = []
    for msg in messages:
        if isinstance(msg, UserMessage):
            lines.append(f"Contractor: {msg.content}")
        elif isinstance(msg, AssistantMessage) and msg.content:
            lines.append(f"Assistant: {msg.content}")
    return "\n".join(lines)


def _parse_compaction_response(raw: str) -> list[dict[str, str]]:
    """Parse the LLM compaction response into a list of fact dicts.

    Handles common LLM formatting issues like markdown code fences.
    Returns an empty list if parsing fails.
    """
    text = raw.strip()
    # Strip markdown code fences if present
    if text.startswith("```"):
        # Remove opening fence (with optional language tag)
        first_newline = text.index("\n") if "\n" in text else len(text)
        text = text[first_newline + 1 :]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("Failed to parse compaction response as JSON: %s", text[:200])
        return []

    if not isinstance(parsed, list):
        logger.warning("Compaction response is not a JSON array")
        return []

    valid_categories = {"pricing", "client", "job", "supplier", "scheduling", "general"}
    facts: list[dict[str, str]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        key = item.get("key", "")
        value = item.get("value", "")
        category = item.get("category", "general")
        if not key or not value:
            continue
        if category not in valid_categories:
            category = "general"
        facts.append({"key": str(key), "value": str(value), "category": str(category)})
    return facts


async def compact_session(
    db: Session,
    contractor_id: int,
    trimmed_messages: list[AgentMessage],
    max_message_id: int | None = None,
) -> tuple[list[dict[str, str]], int | None]:
    """Extract durable facts from messages about to leave the context window.

    Uses a lightweight LLM call to identify facts worth persisting, then saves
    them via the existing memory subsystem.

    Args:
        db: Database session.
        contractor_id: The contractor whose session is being compacted.
        trimmed_messages: Messages that are about to be dropped from context.
        max_message_id: The highest message ID among the trimmed messages,
            used to track compaction progress. Passed through to the return value.

    Returns:
        A tuple of (saved_facts, max_message_id) where saved_facts is a list of
        dicts with "key", "value", and "category" fields, and max_message_id is
        the highest compacted message ID (for tracking).
    """
    if not trimmed_messages:
        return [], None

    if not settings.compaction_enabled:
        return [], None

    conversation_text = _format_messages_for_compaction(trimmed_messages)
    if not conversation_text.strip():
        return [], None

    model = settings.compaction_model or settings.llm_model
    provider = settings.compaction_provider or settings.llm_provider

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": COMPACTION_SYSTEM_PROMPT},
        {"role": "user", "content": conversation_text},
    ]

    try:
        response = cast(
            ChatCompletion,
            await acompletion(
                model=model,
                provider=provider,
                api_base=settings.llm_api_base,
                messages=messages,  # type: ignore[arg-type]
                max_tokens=settings.compaction_max_tokens,
            ),
        )
    except Exception:
        logger.exception("Compaction LLM call failed for contractor %d", contractor_id)
        return [], None

    raw_content = response.choices[0].message.content or ""
    facts = _parse_compaction_response(raw_content)

    saved_facts: list[dict[str, str]] = []
    for fact in facts:
        try:
            await save_memory(
                db,
                contractor_id=contractor_id,
                key=fact["key"],
                value=fact["value"],
                category=fact["category"],
                confidence=0.8,
            )
            saved_facts.append(fact)
            logger.info(
                "Compaction saved fact for contractor %d: %s = %s",
                contractor_id,
                fact["key"],
                fact["value"][:80],
            )
        except Exception:
            logger.exception(
                "Failed to save compacted fact %s for contractor %d",
                fact["key"],
                contractor_id,
            )

    return saved_facts, max_message_id
