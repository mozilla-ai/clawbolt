"""Session compaction: consolidate aging messages into MEMORY.md.

When conversation history reaches the configured limit, messages about to be
trimmed are passed through a lightweight LLM call that rewrites MEMORY.md
with any new durable facts from the conversation.  This replaces the old
fact-extraction approach with a full-rewrite model (like nanobot).

A timestamped summary is also appended to HISTORY.md so the conversation
remains searchable after the raw messages are gone.
"""

import datetime
import json
import logging
from typing import Any, cast

from any_llm import amessages
from any_llm.types.messages import MessageResponse

from backend.app.agent.llm_parsing import get_response_text
from backend.app.agent.memory_db import get_memory_store
from backend.app.agent.messages import AgentMessage, AssistantMessage, UserMessage
from backend.app.agent.prompts import load_prompt
from backend.app.config import settings
from backend.app.services.llm_service import reasoning_effort_to_thinking

logger = logging.getLogger(__name__)

COMPACTION_SYSTEM_PROMPT = load_prompt("compaction")


def _format_messages_for_compaction(messages: list[AgentMessage]) -> str:
    """Format a list of agent messages into a readable text block for the LLM."""
    lines: list[str] = []
    for msg in messages:
        if isinstance(msg, UserMessage):
            lines.append(f"User: {msg.content}")
        elif isinstance(msg, AssistantMessage) and msg.content:
            lines.append(f"Assistant: {msg.content}")
    return "\n".join(lines)


def _parse_compaction_response(raw: str) -> tuple[str, str]:
    """Parse the LLM compaction response into a memory update and summary.

    The assistant prefill starts the response with ``{``, so the raw text from
    the LLM may be missing the leading brace.  We try the text as-is first,
    then retry with a prepended ``{`` before giving up.

    Returns a tuple of (memory_update, summary_string).
    """
    text = raw.strip()
    # Strip markdown code fences if present
    if text.startswith("```"):
        first_newline = text.index("\n") if "\n" in text else len(text)
        text = text[first_newline + 1 :]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

    # Try parsing as-is first, then with prepended "{" (from assistant prefill)
    parsed = None
    for candidate in (text, "{" + text):
        try:
            parsed = json.loads(candidate)
            break
        except json.JSONDecodeError:
            continue

    if parsed is None:
        logger.warning("Failed to parse compaction response as JSON: %s", text[:200])
        return "", ""

    if not isinstance(parsed, dict):
        logger.warning("Compaction response is not a JSON object")
        return "", ""

    memory_update = str(parsed.get("memory_update", "")).strip()
    summary = str(parsed.get("summary", "")).strip()
    return memory_update, summary


async def compact_session(
    user_id: str,
    trimmed_messages: list[AgentMessage],
    max_message_seq: int | None = None,
) -> tuple[str, int | None]:
    """Consolidate messages into an updated MEMORY.md via LLM rewrite.

    Passes the current MEMORY.md, USER.md, and the conversation to the LLM,
    which returns a full rewritten MEMORY.md incorporating any new facts.

    Args:
        user_id: The user whose session is being compacted.
        trimmed_messages: Messages that are about to be dropped from context.
        max_message_seq: The highest message seq among the trimmed messages,
            used to track compaction progress. Passed through to the return value.

    Returns:
        A tuple of (memory_update, max_message_seq) where memory_update is the
        new MEMORY.md content (empty string if nothing changed), and
        max_message_seq is the highest compacted message seq (for tracking).
    """
    if not trimmed_messages:
        return "", None

    if not settings.compaction_enabled:
        return "", None

    conversation_text = _format_messages_for_compaction(trimmed_messages)
    if not conversation_text.strip():
        return "", None

    memory_store = get_memory_store(user_id)
    current_memory = memory_store.read_memory()
    current_user_profile = memory_store.read_user()

    user_prompt_parts = []
    user_prompt_parts.append("## Current Long-term Memory")
    user_prompt_parts.append(current_memory or "(empty)")
    user_prompt_parts.append("")
    user_prompt_parts.append("## User Profile (USER.md)")
    user_prompt_parts.append(current_user_profile or "(empty)")
    user_prompt_parts.append("")
    user_prompt_parts.append("## Conversation to Process")
    user_prompt_parts.append(conversation_text)

    model = settings.compaction_model or settings.llm_model
    provider = settings.compaction_provider or settings.llm_provider

    messages: list[dict[str, Any]] = [
        {"role": "user", "content": "\n".join(user_prompt_parts)},
    ]

    try:
        response = cast(
            MessageResponse,
            await amessages(
                model=model,
                provider=provider,
                api_base=settings.llm_api_base,
                system=COMPACTION_SYSTEM_PROMPT,
                messages=messages,
                max_tokens=settings.compaction_max_tokens,
                thinking=reasoning_effort_to_thinking(settings.reasoning_effort),
            ),
        )
    except Exception:
        logger.exception("Compaction LLM call failed for user %s", user_id)
        return "", None

    raw_content = get_response_text(response)
    memory_update, summary = _parse_compaction_response(raw_content)

    # Write updated MEMORY.md if the LLM produced content
    if memory_update:
        memory_store.write_memory(memory_update)
        logger.info("Compaction rewrote MEMORY.md for user %s", user_id)

    # Append summary to HISTORY.md if the LLM produced one
    if summary:
        timestamp = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d %H:%M")
        entry = summary.replace("[TIMESTAMP]", f"[{timestamp}]")
        try:
            await memory_store.append_history(entry)
            logger.info("Compaction appended history entry for user %s", user_id)
        except Exception:
            logger.exception("Failed to append history for user %s", user_id)

    return memory_update, max_message_seq
