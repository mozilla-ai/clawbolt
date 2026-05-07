"""Session compaction: consolidate aging messages into persistent files.

When conversation history reaches the configured limit, messages about to be
trimmed are passed through a lightweight LLM call that updates MEMORY.md,
USER.md, and SOUL.md with any new durable facts from the conversation.

A timestamped summary is also appended to HISTORY.md so the conversation
remains searchable after the raw messages are gone.
"""

import datetime
import hashlib
import json
import logging
import time
from datetime import UTC
from typing import Any, cast

from any_llm import amessages
from any_llm.types.messages import MessageResponse

from backend.app.agent.llm_parsing import get_response_text
from backend.app.agent.memory_db import get_memory_store
from backend.app.agent.messages import AgentMessage, AssistantMessage, UserMessage
from backend.app.agent.observer import (
    PURPOSE_COMPACTION,
    LLMRequestPayload,
    emit_llm_request,
)
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

# Snapshot truncation hint sizes. The head and tail are full plaintext so an
# admin reviewing a truncation record can still read the start and end of
# what changed; the sha256 lets a determined operator compare two records
# without storing the full body twice.
_SNAPSHOT_HEAD_BYTES = 2_000
_SNAPSHOT_TAIL_BYTES = 2_000


def _serialize_snapshot(text: str | None, cap: int) -> str | None:
    """Return *text* itself if under *cap* bytes, else a truncation record.

    The returned string is what eventually lands in a ``compaction_events``
    encrypted column. ``None`` in, ``None`` out (for the
    skip-if-nothing-to-store path). When *text* exceeds *cap* bytes encoded
    as UTF-8, returns a JSON record with ``truncated``, ``size_bytes``,
    ``head``, ``tail`` and ``sha256`` so admins can still see the shape of
    what was compacted without storing the full body. The cap bounds the
    worst-case row size at roughly ``2 * (HEAD + TAIL + sha256 overhead)``
    per file, regardless of how large MEMORY.md grows.
    """
    if text is None:
        return None
    encoded = text.encode("utf-8")
    if len(encoded) <= cap:
        return text
    digest = hashlib.sha256(encoded).hexdigest()
    head = encoded[:_SNAPSHOT_HEAD_BYTES].decode("utf-8", errors="replace")
    tail = encoded[-_SNAPSHOT_TAIL_BYTES:].decode("utf-8", errors="replace")
    return json.dumps(
        {
            "truncated": True,
            "size_bytes": len(encoded),
            "head": head,
            "tail": tail,
            "sha256": digest,
        },
        ensure_ascii=False,
    )


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
    event_id: int | None = None,
    admin_note: str | None = None,
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
        event_id: When provided, the existing ``compaction_events`` row to
            update with snapshots and flip from ``'pending'`` to
            ``'completed'``. Set by ``trigger_compaction_for_dropped`` which
            pre-inserts the row in the same transaction that advances the
            trim watermark. When ``None`` (e.g. test invocations or any
            future caller that has not pre-inserted), a new completed row
            is inserted.
        admin_note: Optional steering note prepended to the conversation
            block as ``[admin note: ...]`` so the compaction LLM can be
            biased about how to read the conversation. Used by the admin
            "compact now" path to flag e.g. "the agent made factual errors
            about its own capabilities; do not preserve those as facts".
            Has no effect on the trim-driven hot path, which leaves it
            unset.

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

    if admin_note:
        conversation_text = f"[admin note: {admin_note}]\n\n{conversation_text}"

    # Telemetry: compaction is a routine operation for active users (every
    # ~27 days at 15k tokens/day, more often for power users). Capturing
    # per-run shape so we can audit frequency, cost, and whether the LLM
    # is actually picking up new facts vs producing empty rewrites.
    _start_monotonic = time.monotonic()
    _trimmed_count = len(trimmed_messages)
    _input_chars = sum(len(m.content or "") for m in trimmed_messages if hasattr(m, "content"))

    memory_store = get_memory_store(user_id)
    current_memory = await memory_store.read_memory_async()
    current_user_profile = await memory_store.read_user_async()
    current_soul = await memory_store.read_soul_async()
    current_history = await memory_store.read_history_async()
    heartbeat_store = HeartbeatStore(user_id)
    current_heartbeat = await heartbeat_store.read_heartbeat_md_async()

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
    compaction_system = prepare_system_with_caching(COMPACTION_SYSTEM_PROMPT)
    compaction_thinking = reasoning_effort_to_thinking(settings.reasoning_effort)

    try:
        await emit_llm_request(
            LLMRequestPayload(
                schema_version=1,
                purpose=PURPOSE_COMPACTION,
                user_id=user_id,
                session_id=None,
                request_id=None,
                model=model,
                provider=provider,
                max_tokens=settings.compaction_max_tokens,
                thinking=compaction_thinking,
                system=compaction_system,
                messages=messages,
                tools=None,
                # Compaction operates on a synthetic prompt rebuilt from
                # MEMORY/USER/SOUL/HEARTBEAT plus the trimmed conversation
                # text, not on a session-aware message history. The
                # era-marker field has no meaning here.
                min_message_seq_in_prompt=None,
                started_at=datetime.datetime.now(UTC),
            )
        )
        response = cast(
            MessageResponse,
            await amessages(
                model=model,
                provider=provider,
                api_base=settings.llm_api_base,
                system=compaction_system,
                messages=messages,
                max_tokens=settings.compaction_max_tokens,
                thinking=compaction_thinking,
            ),
        )
    except Exception:
        logger.exception("Compaction LLM call failed for user %s", user_id)
        return "", None

    await log_llm_usage(user_id, model, response, purpose="compaction", provider=provider)

    raw_content = get_response_text(response)
    result = _parse_compaction_response(raw_content)

    # Capture exactly what got appended to HISTORY.md so we can compute the
    # "after" snapshot deterministically below. ``None`` means no append
    # happened this event.
    appended_history_entry: str | None = None

    # Write updated MEMORY.md if the LLM produced content
    if result.memory_update:
        await memory_store.write_memory_async(result.memory_update)
        logger.info("Compaction rewrote MEMORY.md for user %s", user_id)

    # Append summary to HISTORY.md if the LLM produced one
    if result.summary:
        timestamp = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d %H:%M")
        entry = result.summary.replace("[TIMESTAMP]", f"[{timestamp}]")
        try:
            await memory_store.append_history(entry)
            # Mirror the suffix that ``MemoryStore.append_history`` adds at
            # the SQL level so the deterministic snapshot below matches.
            appended_history_entry = entry + "\n"
            logger.info("Compaction appended history entry for user %s", user_id)
        except Exception:
            logger.exception("Failed to append history for user %s", user_id)

    # Write updated USER.md if the LLM detected new profile info
    if result.user_profile_update:
        await memory_store.write_user_async(result.user_profile_update)
        logger.info("Compaction updated USER.md for user %s", user_id)

    # Write updated SOUL.md if the LLM detected personality changes
    if result.soul_update:
        await memory_store.write_soul_async(result.soul_update)
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

    # Compute "after" snapshots deterministically from what was written
    # rather than re-reading the memory store. Two compact_session tasks
    # for the same user can run concurrently (e.g. burst traffic crosses
    # the trigger again during a still-running LLM compaction call), and
    # they share ``get_memory_store(user_id)``. A re-read could pick up the
    # other task's write and record a misleading "after" in this row's
    # audit log. The compaction prompt returns full rewrites for memory /
    # user / soul, and ``append_history`` is a SQL-level concatenation we
    # mirror via ``appended_history_entry`` above, so all four "after"
    # values are computable without re-reading.
    new_memory = result.memory_update if result.memory_update else current_memory
    new_user = result.user_profile_update if result.user_profile_update else current_user_profile
    new_soul = result.soul_update if result.soul_update else current_soul
    if appended_history_entry is not None:
        new_history = (current_history or "") + appended_history_entry
    else:
        new_history = current_history

    cap = settings.compaction_event_snapshot_max_bytes_per_file
    snapshots = _build_snapshot_pairs(
        cap=cap,
        memory_before=current_memory,
        memory_after=new_memory,
        history_before=current_history,
        history_after=new_history,
        user_before=current_user_profile,
        user_after=new_user,
        soul_before=current_soul,
        soul_after=new_soul,
    )
    # Capture the LLM call itself for Layer 5 admin observability:
    # the trimmed conversation that was sent (the static system prompt
    # and the four current memory files are excluded; the system prompt
    # is identical across events and the memory inputs are already in
    # the ``*_text_before`` snapshots), the unparsed response text, and
    # the parsed fields as a JSON string. All three share the per-file
    # truncation cap so an unusually long conversation does not blow up
    # the row.
    parsed_response_json = json.dumps(
        {
            "memory_update": result.memory_update,
            "summary": result.summary,
            "user_profile_update": result.user_profile_update,
            "soul_update": result.soul_update,
        },
        ensure_ascii=False,
    )
    llm_call = {
        "prompt_text": _serialize_snapshot(conversation_text, cap),
        "raw_response_text": _serialize_snapshot(raw_content, cap),
        "parsed_response_json": _serialize_snapshot(parsed_response_json, cap),
    }

    # Persist the metrics + snapshots. Either UPDATE the pending row that
    # ``trigger_compaction_for_dropped`` pre-inserted (event_id provided),
    # or INSERT a fresh completed row (legacy/test paths). A DB hiccup
    # here must not lose the compacted memory we just wrote upstream.
    try:
        await _persist_compaction_event(
            event_id=event_id,
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
            snapshots=snapshots,
            llm_call=llm_call,
        )
    except Exception:
        logger.exception("Failed to persist compaction event for user %s", user_id)

    return result.memory_update, max_message_seq


def _build_snapshot_pairs(
    *,
    cap: int,
    memory_before: str,
    memory_after: str,
    history_before: str,
    history_after: str,
    user_before: str,
    user_after: str,
    soul_before: str,
    soul_after: str,
) -> dict[str, str | None]:
    """Apply the truncation cap and the skip-if-unchanged optimization.

    Returns a dict keyed by ``CompactionEvent`` column name. A column whose
    before and after match (file unchanged this event) maps to ``None`` so
    the persist path leaves both columns NULL and saves the encryption
    overhead plus the row bytes.
    """
    pairs: dict[str, str | None] = {
        "memory_text_before": None,
        "memory_text_after": None,
        "history_text_before": None,
        "history_text_after": None,
        "user_text_before": None,
        "user_text_after": None,
        "soul_text_before": None,
        "soul_text_after": None,
    }
    for prefix, before, after in (
        ("memory_text", memory_before, memory_after),
        ("history_text", history_before, history_after),
        ("user_text", user_before, user_after),
        ("soul_text", soul_before, soul_after),
    ):
        if before == after:
            continue
        pairs[f"{prefix}_before"] = _serialize_snapshot(before, cap)
        pairs[f"{prefix}_after"] = _serialize_snapshot(after, cap)
    return pairs


async def _persist_compaction_event(
    *,
    event_id: int | None,
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
    snapshots: dict[str, str | None],
    llm_call: dict[str, str | None],
) -> None:
    """Write or update one ``CompactionEvent`` row.

    When ``event_id`` is provided, UPDATE the pre-inserted ``'pending'``
    row (the agent-loop path). Otherwise INSERT a new ``'completed'`` row
    (test / legacy path). ``llm_call`` carries the migration-031 columns
    (``prompt_text``, ``raw_response_text``, ``parsed_response_json``);
    keeping it as a dict mirrors the ``snapshots`` shape so adding more
    audit columns later does not require re-threading positional args.
    Imports SQLAlchemy lazily so the agent module does not pull it at
    import time on every pure-logic test.
    """
    from sqlalchemy import select

    from backend.app.database import db_session_async
    from backend.app.models import CompactionEvent

    async with db_session_async() as db:
        if event_id is not None:
            event = (
                await db.execute(select(CompactionEvent).filter_by(id=event_id))
            ).scalar_one_or_none()
            if event is None:
                logger.warning(
                    "Compaction event id=%d not found for user %s; "
                    "inserting a fresh completed row instead",
                    event_id,
                    user_id,
                )
                event_id = None
            else:
                event.trimmed_count = trimmed_count
                event.trimmed_chars = trimmed_chars
                event.input_tokens = input_tokens
                event.output_tokens = output_tokens
                event.duration_ms = duration_ms
                if max_message_seq is not None:
                    event.max_message_seq = max_message_seq
                event.memory_updated = memory_updated
                event.user_profile_updated = user_profile_updated
                event.soul_updated = soul_updated
                event.summary_len = summary_len
                event.status = "completed"
                for col, value in snapshots.items():
                    setattr(event, col, value)
                for col, value in llm_call.items():
                    setattr(event, col, value)
        if event_id is None:
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
                    status="completed",
                    **snapshots,
                    **llm_call,
                )
            )
        await db.commit()
