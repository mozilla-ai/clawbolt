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
import re
import time
from collections import Counter
from datetime import UTC
from typing import Any, cast

from any_llm import amessages
from any_llm.types.messages import MessageResponse

from backend.app.agent.llm_parsing import get_response_text
from backend.app.agent.markdown_registry import BudgetExceededError
from backend.app.agent.memory_db import get_memory_store
from backend.app.agent.messages import AgentMessage, AssistantMessage, UserMessage
from backend.app.agent.observer import (
    PURPOSE_COMPACTION,
    LLMRequestPayload,
    LLMResponsePayload,
    emit_llm_request,
    emit_llm_response,
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


def _line_diff_counts(before: str, after: str) -> tuple[int, int]:
    """Multiset line diff: ``(added, removed)`` counts between two texts.

    Cheap deterministic signal for memory erosion across compaction
    rewrites (issue #1433): the rewrite model can silently drop a valid
    line on any cycle, and the compliance audit explicitly encourages
    deletion, so erosion looks like a large removed count not matched by
    additions. Counter-based and order-insensitive: a pure reorder
    reports ``(0, 0)``. Surfaced on the ``compaction.summary`` log line
    so an aggregator can alert on unexplained large removals.
    """
    before_counts = Counter(before.splitlines())
    after_counts = Counter(after.splitlines())
    added = sum((after_counts - before_counts).values())
    removed = sum((before_counts - after_counts).values())
    return added, removed


_URL_RE = re.compile(r"https?://\S+")


def _strip_assistant_noise(text: str) -> str:
    """Strip URLs from an assistant reply before sending it to the compactor.

    Assistant replies often quote tool-receipt links (CompanyCam photo URLs,
    QBO deep links, AppFolio work-order URLs) verbatim alongside the actual
    durable content. The URLs are operational chatter the compactor wastes
    context summarizing. The semantic prose around them is what we care
    about. User messages are left alone, since URLs the contractor pastes
    are usually intentional.
    """
    return _URL_RE.sub("", text)


def _format_messages_for_compaction(messages: list[AgentMessage]) -> str:
    """Format a list of agent messages into a readable text block for the LLM."""
    lines: list[str] = []
    for msg in messages:
        if isinstance(msg, UserMessage):
            lines.append(f"User: {msg.content}")
        elif isinstance(msg, AssistantMessage) and msg.content:
            cleaned = _strip_assistant_noise(msg.content)
            lines.append(f"Assistant: {cleaned}")
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


async def _emit_compaction_response(
    response: MessageResponse,
    *,
    user_id: str,
    model: str,
    provider: str,
    started_at: datetime.datetime,
) -> None:
    """Build and dispatch an ``LLMResponsePayload`` for the compaction call.

    Mirrors ``ClawboltAgent._emit_response`` for the agent-loop side; the
    same serialization rules apply (plain dicts via ``model_dump``, per-block
    try/except so a single unexpected block shape cannot break the dispatch).
    Compaction's session_id / request_id are ``None`` because compaction
    runs on a synthetic prompt outside the session-aware path.
    """
    content_blocks: list[dict[str, Any]] = []
    for block in response.content:
        try:
            content_blocks.append(block.model_dump(mode="json"))
        except Exception:
            logger.exception(
                "Failed to serialize compaction response content block; skipping for observer"
            )
    usage = response.usage
    await emit_llm_response(
        LLMResponsePayload(
            schema_version=1,
            purpose=PURPOSE_COMPACTION,
            user_id=user_id,
            session_id=None,
            request_id=None,
            model=model,
            provider=provider,
            content_blocks=content_blocks,
            stop_reason=response.stop_reason,
            input_tokens=usage.input_tokens if usage else None,
            output_tokens=usage.output_tokens if usage else None,
            cache_creation_input_tokens=(usage.cache_creation_input_tokens if usage else None),
            cache_read_input_tokens=(usage.cache_read_input_tokens if usage else None),
            started_at=started_at,
            completed_at=datetime.datetime.now(UTC),
        )
    )


async def compact_session(
    user_id: str,
    trimmed_messages: list[AgentMessage],
    max_message_seq: int | None = None,
    event_id: int | None = None,
    admin_note: str | None = None,
    hygiene_only: bool = False,
) -> tuple[str, int | None]:
    """Consolidate messages into an updated MEMORY.md via LLM rewrite.

    Passes the current MEMORY.md, USER.md, SOUL.md, HEARTBEAT.md, and the
    conversation to the LLM, which returns a full rewritten MEMORY.md
    incorporating any new facts.

    Args:
        user_id: The user whose session is being compacted.
        trimmed_messages: Messages that are about to be dropped from context.
            Ignored when *hygiene_only* is True.
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
        hygiene_only: When True, skip the conversation-message requirement
            and instead run the compliance audit only. The LLM re-audits
            the existing MEMORY.md against the Do-Not-Include list without
            needing new conversation content. ``trimmed_messages`` is
            ignored in this mode.

    Returns:
        A tuple of (memory_update, max_message_seq) where memory_update is the
        new MEMORY.md content (empty string if nothing changed), and
        max_message_seq is the highest compacted message seq (for tracking).
    """
    if not trimmed_messages and not hygiene_only:
        return "", None

    if not settings.compaction_enabled:
        return "", None

    if hygiene_only:
        # If memory is empty, there is nothing to audit.
        memory_store = get_memory_store(user_id)
        current_memory_check = await memory_store.read_memory_async()
        if not current_memory_check or not current_memory_check.strip():
            return "", None

        # Build a minimal conversation block that triggers the compliance
        # audit in Step 1 of the prompt without providing actual messages.
        # The model reads this and performs the compliance audit on the
        # existing MEMORY.md, then merges nothing because there are no
        # new facts.
        conversation_text = (
            "[compliance audit: re-audit existing MEMORY.md against the Do-Not-Include list]"
        )
        _trimmed_count = 0
        _input_chars = 0
    else:
        conversation_text = _format_messages_for_compaction(trimmed_messages)
        if not conversation_text.strip():
            return "", None
        _trimmed_count = len(trimmed_messages)
        _input_chars = sum(len(m.content or "") for m in trimmed_messages if hasattr(m, "content"))

    if admin_note:
        conversation_text = f"[admin note: {admin_note}]\n\n{conversation_text}"

    # Telemetry: compaction is a routine operation for active users (every
    # ~27 days at 15k tokens/day, more often for power users). Capturing
    # per-run shape so we can audit frequency, cost, and whether the LLM
    # is actually picking up new facts vs producing empty rewrites.
    _start_monotonic = time.monotonic()

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

    started_at = datetime.datetime.now(UTC)
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
                started_at=started_at,
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
    await _emit_compaction_response(
        response,
        user_id=user_id,
        model=model,
        provider=provider,
        started_at=started_at,
    )

    await log_llm_usage(user_id, model, response, purpose="compaction", provider=provider)

    raw_content = get_response_text(response)
    result = _parse_compaction_response(raw_content)

    # An LLM that echoes existing content verbatim is not a memory change.
    # ``.strip()`` ignores trailing-whitespace noise that ``write_*_async``
    # would normalize anyway.
    memory_changed = (
        bool(result.memory_update)
        and result.memory_update.strip() != (current_memory or "").strip()
    )
    user_changed = (
        bool(result.user_profile_update)
        and result.user_profile_update.strip() != (current_user_profile or "").strip()
    )
    soul_changed = (
        bool(result.soul_update) and result.soul_update.strip() != (current_soul or "").strip()
    )

    # Track the post-append HISTORY text for the audit snapshot. Stays
    # equal to ``current_history`` when no entry was appended this event.
    new_history: str = current_history

    # Write updated MEMORY.md only when the rewrite actually differs.
    # An LLM that returns a rewrite over the bounded-growth byte budget
    # (see ``backend/app/agent/markdown_registry.py``) is treated as a
    # failed compaction for that file: log a warning and keep the
    # current memory rather than truncating mid-sentence and producing
    # silently corrupt durable state. The conversation that triggered
    # this compaction will still trim, and the next compaction will get
    # another chance to produce an in-budget rewrite.
    if memory_changed:
        try:
            await memory_store.write_memory_async(result.memory_update)
            logger.info("Compaction rewrote MEMORY.md for user %s", user_id)
        except BudgetExceededError as exc:
            logger.warning(
                "Compaction skipping MEMORY.md update for user %s: %s",
                user_id,
                exc,
            )
            memory_changed = False

    # Append summary to HISTORY.md if the LLM produced one. ``append_history``
    # returns the new full text under the same row-level lock that protected
    # the read-and-write, so the snapshot we record matches what landed in
    # the DB even when two compactions race.
    if result.summary:
        # Real event times come from the model, anchored to the conversation's
        # [Weekday, ...] markers (see compaction.md Step 4). Any leftover
        # [TIMESTAMP] placeholder means the model saw no marker for that event;
        # fill it with the compaction time as a fallback.
        fallback_ts = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d %H:%M")
        entry = result.summary.replace("[TIMESTAMP]", f"[{fallback_ts}]")
        try:
            new_history = await memory_store.append_history(entry)
            logger.info("Compaction appended history entry for user %s", user_id)
        except Exception:
            logger.exception("Failed to append history for user %s", user_id)

    # Write updated USER.md only when the rewrite actually differs.
    # See MEMORY.md branch above for the BudgetExceededError handling.
    if user_changed:
        try:
            await memory_store.write_user_async(result.user_profile_update)
            logger.info("Compaction updated USER.md for user %s", user_id)
        except BudgetExceededError as exc:
            logger.warning(
                "Compaction skipping USER.md update for user %s: %s",
                user_id,
                exc,
            )
            user_changed = False

    # Write updated SOUL.md only when the rewrite actually differs.
    if soul_changed:
        try:
            await memory_store.write_soul_async(result.soul_update)
            logger.info("Compaction updated SOUL.md for user %s", user_id)
        except BudgetExceededError as exc:
            logger.warning(
                "Compaction skipping SOUL.md update for user %s: %s",
                user_id,
                exc,
            )
            soul_changed = False

    # Single structured summary line. Fields are space-separated key=value
    # so log aggregators (Railway, Loki) can group / filter without
    # needing JSON. ``input_tokens`` reflects the tokens Anthropic
    # billed; the ``trimmed_chars`` field gives a provider-agnostic
    # input-size proxy. ``*_updated`` flags reflect real persisted
    # diffs: an LLM that returns content identical to what was already
    # on disk produces ``False`` here, not ``True``.
    _input_tokens = response.usage.input_tokens or 0 if response.usage else 0
    _output_tokens = response.usage.output_tokens or 0 if response.usage else 0
    _duration_ms = int((time.monotonic() - _start_monotonic) * 1000)
    # Erosion signal: how many MEMORY.md lines this rewrite added and
    # removed. (0, 0) when the file did not change this event.
    _memory_lines_added, _memory_lines_removed = (
        _line_diff_counts(current_memory or "", result.memory_update) if memory_changed else (0, 0)
    )
    logger.info(
        "compaction.summary user=%s trimmed_count=%d trimmed_chars=%d "
        "input_tokens=%d output_tokens=%d duration_ms=%d "
        "memory_updated=%s user_updated=%s soul_updated=%s summary_len=%d "
        "memory_lines_added=%d memory_lines_removed=%d",
        user_id,
        _trimmed_count,
        _input_chars,
        _input_tokens,
        _output_tokens,
        _duration_ms,
        memory_changed,
        user_changed,
        soul_changed,
        len(result.summary or ""),
        _memory_lines_added,
        _memory_lines_removed,
    )

    # Compute "after" snapshots deterministically from what was written
    # rather than re-reading the memory store. Two compact_session tasks
    # for the same user can run concurrently (e.g. burst traffic crosses
    # the trigger again during a still-running LLM compaction call), and
    # they share ``get_memory_store(user_id)``. A re-read could pick up the
    # other task's write and record a misleading "after" in this row's
    # audit log. The compaction prompt returns full rewrites for memory /
    # user / soul, and ``append_history`` returns the row's new full
    # plaintext under the same row-level lock that wrote it, so all four
    # "after" values are computable without re-reading.
    new_memory = result.memory_update if memory_changed else current_memory
    new_user = result.user_profile_update if user_changed else current_user_profile
    new_soul = result.soul_update if soul_changed else current_soul

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
            memory_updated=memory_changed,
            user_profile_updated=user_changed,
            soul_updated=soul_changed,
            summary_len=len(result.summary or ""),
            snapshots=snapshots,
            llm_call=llm_call,
        )
    except Exception:
        logger.exception("Failed to persist compaction event for user %s", user_id)

    return (result.memory_update if memory_changed else ""), max_message_seq


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
