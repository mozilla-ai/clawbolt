"""Session compaction: consolidate aging messages into persistent files.

When conversation history reaches the configured limit, messages about to be
trimmed are passed through a lightweight LLM call that updates MEMORY.md,
USER.md, and SOUL.md with any new durable facts from the conversation.

A timestamped summary is also appended to HISTORY.md so the conversation
remains searchable after the raw messages are gone.
"""

import datetime
import json
import logging
import time
from typing import Any, cast

from any_llm import amessages
from any_llm.types.messages import MessageResponse

from backend.app.agent.llm_parsing import get_response_text
from backend.app.agent.memory_db import get_memory_store
from backend.app.agent.messages import AgentMessage, AssistantMessage, UserMessage
from backend.app.agent.prompts import load_prompt
from backend.app.agent.stores import HeartbeatStore
from backend.app.config import settings
from backend.app.services.llm_service import (
    prepare_system_with_caching,
    reasoning_effort_to_thinking,
)
from backend.app.services.llm_usage import log_llm_usage

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


class CompactionResult:
    """Parsed result from the compaction LLM response."""

    __slots__ = ("memory_update", "soul_update", "summary", "user_profile_update")

    def __init__(
        self,
        memory_update: str = "",
        summary: str = "",
        user_profile_update: str = "",
        soul_update: str = "",
    ) -> None:
        self.memory_update = memory_update
        self.summary = summary
        self.user_profile_update = user_profile_update
        self.soul_update = soul_update

    def __setattr__(self, name: str, value: str) -> None:
        if hasattr(self, name):
            raise AttributeError(f"CompactionResult is immutable: cannot reassign '{name}'")
        object.__setattr__(self, name, value)


_EMPTY_RESULT = CompactionResult()


def _parse_compaction_response(raw: str) -> CompactionResult:
    """Parse the LLM compaction response into structured updates.

    The assistant prefill starts the response with ``{``, so the raw text from
    the LLM may be missing the leading brace.  We try the text as-is first,
    then retry with a prepended ``{`` before giving up.

    Returns a ``CompactionResult`` with memory, summary, user profile, and
    soul updates. Empty strings indicate no change for that field.
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
        return _EMPTY_RESULT

    if not isinstance(parsed, dict):
        logger.warning("Compaction response is not a JSON object")
        return _EMPTY_RESULT

    return CompactionResult(
        memory_update=str(parsed.get("memory_update", "")).strip(),
        summary=str(parsed.get("summary", "")).strip(),
        user_profile_update=str(parsed.get("user_profile_update", "")).strip(),
        soul_update=str(parsed.get("soul_update", "")).strip(),
    )


async def compact_session(
    user_id: str,
    trimmed_messages: list[AgentMessage],
    max_message_seq: int | None = None,
) -> tuple[str, int | None]:
    """Consolidate messages into an updated MEMORY.md via LLM rewrite.

    Passes the current MEMORY.md, USER.md, SOUL.md, HEARTBEAT.md, and the
    conversation to the LLM, which returns a full rewritten MEMORY.md
    incorporating any new facts.

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

    # Telemetry: compaction is a routine operation for active users (every
    # ~27 days at 15k tokens/day, more often for power users). Capturing
    # per-run shape so we can audit frequency, cost, and whether the LLM
    # is actually picking up new facts vs producing empty rewrites.
    _start_monotonic = time.monotonic()
    _trimmed_count = len(trimmed_messages)
    _input_chars = sum(len(m.content or "") for m in trimmed_messages if hasattr(m, "content"))

    memory_store = get_memory_store(user_id)
    current_memory = memory_store.read_memory()
    current_user_profile = memory_store.read_user()
    current_soul = memory_store.read_soul()
    heartbeat_store = HeartbeatStore(user_id)
    current_heartbeat = heartbeat_store.read_heartbeat_md()

    user_prompt_parts = [
        "<current_memory>",
        current_memory or "(empty)",
        "</current_memory>",
        "",
        "<user_profile>",
        current_user_profile or "(empty)",
        "</user_profile>",
        "",
        "<soul>",
        current_soul or "(empty)",
        "</soul>",
        "",
        "<heartbeat>",
        current_heartbeat or "(empty)",
        "</heartbeat>",
        "",
        "<conversation>",
        conversation_text,
        "</conversation>",
    ]

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
                system=prepare_system_with_caching(COMPACTION_SYSTEM_PROMPT),
                messages=messages,
                max_tokens=settings.compaction_max_tokens,
                thinking=reasoning_effort_to_thinking(settings.reasoning_effort),
            ),
        )
    except Exception:
        logger.exception("Compaction LLM call failed for user %s", user_id)
        return "", None

    log_llm_usage(user_id, model, response, purpose="compaction", provider=provider)

    raw_content = get_response_text(response)
    result = _parse_compaction_response(raw_content)

    # Write updated MEMORY.md if the LLM produced content
    if result.memory_update:
        memory_store.write_memory(result.memory_update)
        logger.info("Compaction rewrote MEMORY.md for user %s", user_id)

    # Append summary to HISTORY.md if the LLM produced one
    if result.summary:
        timestamp = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d %H:%M")
        entry = result.summary.replace("[TIMESTAMP]", f"[{timestamp}]")
        try:
            await memory_store.append_history(entry)
            logger.info("Compaction appended history entry for user %s", user_id)
        except Exception:
            logger.exception("Failed to append history for user %s", user_id)

    # Write updated USER.md if the LLM detected new profile info
    if result.user_profile_update:
        memory_store.write_user(result.user_profile_update)
        logger.info("Compaction updated USER.md for user %s", user_id)

    # Write updated SOUL.md if the LLM detected personality changes
    if result.soul_update:
        memory_store.write_soul(result.soul_update)
        logger.info("Compaction updated SOUL.md for user %s", user_id)

    # Single structured summary line. Fields are space-separated key=value
    # so log aggregators (Railway, Loki) can group / filter without
    # needing JSON. ``input_tokens`` reflects the tokens Anthropic
    # billed; the ``trimmed_chars`` field gives a provider-agnostic
    # input-size proxy. ``*_updated`` flags reveal whether the LLM
    # actually produced content for each file vs returning empty.
    _input_tokens = response.usage.input_tokens or 0 if response.usage else 0
    _output_tokens = response.usage.output_tokens or 0 if response.usage else 0
    _duration_ms = int((time.monotonic() - _start_monotonic) * 1000)
    logger.info(
        "compaction.summary user=%s trimmed_count=%d trimmed_chars=%d "
        "input_tokens=%d output_tokens=%d duration_ms=%d "
        "memory_updated=%s user_updated=%s soul_updated=%s summary_len=%d",
        user_id,
        _trimmed_count,
        _input_chars,
        _input_tokens,
        _output_tokens,
        _duration_ms,
        bool(result.memory_update),
        bool(result.user_profile_update),
        bool(result.soul_update),
        len(result.summary or ""),
    )

    # Persist the same metrics so admins can query "when did this user
    # last compact / how big was the input / what got updated" without
    # grepping Railway logs. Best-effort: a DB hiccup must not lose the
    # compacted memory we just wrote upstream.
    try:
        _persist_compaction_event(
            user_id=user_id,
            trimmed_count=_trimmed_count,
            trimmed_chars=_input_chars,
            input_tokens=_input_tokens,
            output_tokens=_output_tokens,
            duration_ms=_duration_ms,
            max_message_seq=max_message_seq,
            memory_updated=bool(result.memory_update),
            user_profile_updated=bool(result.user_profile_update),
            soul_updated=bool(result.soul_update),
            summary_len=len(result.summary or ""),
        )
    except Exception:
        logger.exception("Failed to persist compaction event for user %s", user_id)

    return result.memory_update, max_message_seq


def _persist_compaction_event(
    *,
    user_id: str,
    trimmed_count: int,
    trimmed_chars: int,
    input_tokens: int,
    output_tokens: int,
    duration_ms: int,
    max_message_seq: int | None,
    memory_updated: bool,
    user_profile_updated: bool,
    soul_updated: bool,
    summary_len: int,
) -> None:
    """Write one ``CompactionEvent`` row.

    Imports the model + SessionLocal lazily so the agent module does
    not pull SQLAlchemy at import time on every test that exercises
    pure compaction logic with mocked LLM calls.
    """
    from backend.app.database import SessionLocal
    from backend.app.models import CompactionEvent

    with SessionLocal() as db:
        db.add(
            CompactionEvent(
                user_id=user_id,
                trimmed_count=trimmed_count,
                trimmed_chars=trimmed_chars,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                duration_ms=duration_ms,
                max_message_seq=max_message_seq,
                memory_updated=memory_updated,
                user_profile_updated=user_profile_updated,
                soul_updated=soul_updated,
                summary_len=summary_len,
            )
        )
        db.commit()
