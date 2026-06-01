"""Conversation context loading and session management."""

import asyncio
import datetime
import json
import logging
from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import select

from backend.app.agent.approval import _parse_approval_response
from backend.app.agent.compaction import compact_session
from backend.app.agent.dto import SessionState
from backend.app.agent.messages import (
    AgentMessage,
    AssistantMessage,
    ToolCallRequest,
    ToolResultMessage,
    UserMessage,
)
from backend.app.agent.session_db import get_session_store
from backend.app.agent.system_prompt import to_local_time
from backend.app.config import settings
from backend.app.database import db_session_async
from backend.app.enums import MessageDirection
from backend.app.models import ChatSession, CompactionEvent

logger = logging.getLogger(__name__)

# Canonical trailers that ``format_approval_message`` /
# ``format_plan_message`` append to every system-issued approval prompt.
# Used to identify approval-prompt rows in stored history so they can be
# filtered out before the LLM sees them (see ``load_history`` for the
# rationale — persisted prompts trained the LLM to produce them as
# prose, creating an infinite-loop UX bug). The list is "old + current"
# on purpose: rows persisted before the prompt rewording need to keep
# matching after the new wording ships, otherwise the issue #1049 fix
# silently regresses on already-poisoned sessions.
_APPROVAL_PROMPT_TRAILERS: tuple[str, ...] = (
    # Current wording: last line of the four-option menu.
    "never: deny and remember",
    # Pre-em-dash-fix wording (briefly used between the prompt rewrite
    # and the punctuation-policy fix). Kept so rows persisted in that
    # window still match the trailer filter.
    "never — deny and remember",
    # Pre-rewording wording, kept for backward compatibility with
    # already-persisted rows in the DB.
    "Reply yes or no (always/never to remember your choice)",
)


def _is_approval_prompt(content: str) -> bool:
    """Return True if *content* ends with any known approval-prompt trailer.

    Helper so the load-history filter can detect rows persisted before
    AND after the prompt-text rewording. Matching is by trailing
    substring, not exact prefix, so receipts or prose appended after
    the menu would not be (incorrectly) flagged.
    """
    rstripped = content.rstrip()
    return any(rstripped.endswith(trailer) for trailer in _APPROVAL_PROMPT_TRAILERS)


DEFAULT_HISTORY_LIMIT = settings.conversation_history_limit

# Strong references to fire-and-forget background tasks so they are not
# garbage-collected before completion.
_background_tasks: set[asyncio.Task[None]] = set()


class StoredToolReceipt(BaseModel):
    """Schema for the optional ``ToolReceipt`` attached to a tool result.

    Write-side tools populate this so plain-text channels can render a
    deterministic, human-readable confirmation line tied to a real deep
    link from the API response.
    """

    action: str = ""
    target: str = ""
    url: str | None = None


class StoredToolInteraction(BaseModel):
    """Schema for tool interaction records stored in StoredMessage.tool_interactions_json."""

    tool_call_id: str = ""
    name: str = ""
    args: dict[str, Any] = Field(default_factory=dict)
    result: str = ""
    is_error: bool = False
    tags: set[str] = Field(default_factory=set, exclude=True)
    receipt: StoredToolReceipt | None = None


def _seqs_from_dropped(dropped: list[AgentMessage]) -> list[int]:
    """Return the persisted ``messages.seq`` values carried by *dropped*.

    Only ``UserMessage`` and ``AssistantMessage`` carry seq, and only when
    they were loaded from the DB (in-memory constructions, e.g. the summary
    placeholder injected by ``trim_messages``, are skipped).
    """
    seqs: list[int] = []
    for m in dropped:
        seq = getattr(m, "seq", None)
        if isinstance(seq, int):
            seqs.append(seq)
    return seqs


async def trigger_compaction_for_dropped(
    user_id: str,
    dropped_messages: list[AgentMessage],
) -> None:
    """Fire compaction for messages that were trimmed from context.

    Called from the agent loop (``process_message``) when ``trim_messages``
    drops messages. Two-phase to keep the watermark and the audit log
    consistent under crash:

    1. Synchronously, in one transaction: insert a ``'pending'``
       ``CompactionEvent`` row (with ``min_message_seq`` /
       ``max_message_seq`` populated) AND advance ``sessions.last_trim_seq``
       to ``max_message_seq``. After this commits, the next inbound's
       ``load_conversation_history`` will already filter out the dropped
       messages, so compaction will not re-fire for the same range.

    2. Asynchronously: ``compact_session`` runs the LLM call, fills in the
       four memory-file before/after snapshots on the same row, and flips
       ``status`` to ``'completed'``. If the async task crashes, the row
       stays ``'pending'`` so an admin (or a CLI replay) can identify the
       seq range whose facts were never extracted. The watermark stays
       advanced regardless: this is the design tradeoff (no per-message
       compaction churn) for losing facts on a crashed compaction call.

    This is the only compaction trigger in the system.
    """
    if not dropped_messages or not settings.compaction_enabled:
        return

    dropped_seqs = _seqs_from_dropped(dropped_messages)
    if not dropped_seqs:
        # All dropped messages were in-memory placeholders (e.g. an injected
        # summary) with no DB rows to watermark against. Nothing to compact.
        return

    min_seq = min(dropped_seqs)
    max_seq = max(dropped_seqs)
    triggered_at = datetime.datetime.now(datetime.UTC)

    # Phase 1: insert + watermark advance, in one transaction.
    event_id: int
    try:
        async with db_session_async() as db:
            event = CompactionEvent(
                user_id=user_id,
                triggered_at=triggered_at,
                status="pending",
                min_message_seq=min_seq,
                max_message_seq=max_seq,
                trimmed_count=len(dropped_messages),
            )
            db.add(event)
            await db.flush()
            assert event.id is not None, "flush() must populate the autoincrement id"
            event_id = event.id

            cs = (
                await db.execute(select(ChatSession).filter_by(user_id=user_id))
            ).scalar_one_or_none()
            if cs is not None:
                current = cs.last_trim_seq or 0
                if max_seq > current:
                    cs.last_trim_seq = max_seq
            await db.commit()
    except Exception:
        logger.exception(
            "Failed to record pending compaction event for user %s; "
            "watermark not advanced and async compaction skipped",
            user_id,
        )
        return

    async def _run_trim_compaction(event_id: int) -> None:
        try:
            saved, _ = await compact_session(
                user_id,
                dropped_messages,
                max_message_seq=max_seq,
                event_id=event_id,
            )
            if saved:
                logger.info(
                    "Trim-based compaction extracted facts from %d dropped "
                    "message(s) for user %s (event_id=%d)",
                    len(dropped_messages),
                    user_id,
                    event_id,
                )
        except Exception:
            logger.exception(
                "Trim-based compaction failed for user %s (event_id=%d); "
                "watermark stays advanced, event row stays 'pending'",
                user_id,
                event_id,
            )

    task = asyncio.create_task(_run_trim_compaction(event_id))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    logger.info(
        "Triggered trim-based compaction for user %s: %d dropped "
        "message(s), seq range [%d, %d], event_id=%d",
        user_id,
        len(dropped_messages),
        min_seq,
        max_seq,
        event_id,
    )


def _parse_tool_interactions(raw: str) -> list[StoredToolInteraction]:
    """Parse tool_interactions_json, returning validated models.

    Each item is validated against ``StoredToolInteraction``. Missing fields
    receive defaults. Items that fail validation entirely are logged and
    skipped so corrupt data never crashes loading.
    """
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            return []
    except (json.JSONDecodeError, TypeError):
        logger.debug("Could not parse tool_interactions_json, falling back to flat text")
        return []

    validated: list[StoredToolInteraction] = []
    for i, item in enumerate(parsed):
        try:
            validated.append(StoredToolInteraction.model_validate(item))
        except Exception:
            logger.warning(
                "Skipping invalid tool interaction record at index %d: %r",
                i,
                item,
            )
    return validated


def _expand_outbound_with_tools(
    tool_interactions: list[StoredToolInteraction],
    reply_text: str,
    seq: int | None = None,
) -> list[AgentMessage]:
    """Expand an outbound message with tool interactions into typed messages.

    Reconstructs the message sequence the LLM originally produced:
    1. AssistantMessage with tool_calls (what the LLM requested)
    2. ToolResultMessage for each tool result
    3. AssistantMessage with the final reply text

    *seq* is the source DB row's ``messages.seq``. When provided, both
    AssistantMessage instances carry it, so the trim watermark write in
    ``trigger_compaction_for_dropped`` can find the right value regardless
    of which one ends up in the dropped list. ``ToolResultMessage`` does
    not carry seq because tool results live inside the parent's
    ``tool_interactions_json`` and share the parent's row.
    """
    messages: list[AgentMessage] = []

    # Build ToolCallRequest objects from the stored records
    tool_call_requests: list[ToolCallRequest] = []
    for tc in tool_interactions:
        tool_call_requests.append(
            ToolCallRequest(
                id=tc.tool_call_id,
                name=tc.name,
                arguments=tc.args,
            )
        )

    # AssistantMessage requesting the tool calls (content is typically None)
    messages.append(AssistantMessage(content=None, tool_calls=tool_call_requests, seq=seq))

    # ToolResultMessages for each tool execution
    for tc in tool_interactions:
        messages.append(
            ToolResultMessage(
                tool_call_id=tc.tool_call_id,
                content=tc.result,
            )
        )

    # Final AssistantMessage with the reply text
    messages.append(AssistantMessage(content=reply_text, seq=seq))

    return messages


# Minimum elapsed time between two consecutive visible messages before we
# annotate the later one with a timestamp. Below this, consecutive turns are
# treated as a continuous exchange and left unmarked to keep history terse.
_TIMESTAMP_GAP_THRESHOLD = datetime.timedelta(minutes=30)


def _time_marker(prev_iso: str, cur_iso: str, tz_name: str) -> str | None:
    """Return a localized timestamp marker for a message, or ``None`` to skip.

    A marker is emitted only when the message is the first in the slice
    (``prev_iso`` empty), is separated from the previous visible message by
    more than ``_TIMESTAMP_GAP_THRESHOLD``, or crosses a local-day boundary.
    This surfaces the useful signal (a conversation resumed after a break)
    without prefixing every turn.

    Markers are *absolute* (not relative like "3 hours ago") so a given
    historical message always renders the same string and never busts the
    system/history prompt cache. The current time is injected separately into
    the live user message, so the LLM can compute "how long ago" itself.
    """
    try:
        cur = datetime.datetime.fromisoformat(cur_iso)
    except (ValueError, TypeError):
        return None
    # Stored timestamps are UTC-aware (Message.timestamp is DateTime(timezone=True)),
    # but normalize any naive value to UTC so the gap subtraction below can never
    # raise on a naive/aware mix. History load is on the per-message hot path.
    if cur.tzinfo is None:
        cur = cur.replace(tzinfo=datetime.UTC)
    prev: datetime.datetime | None = None
    if prev_iso:
        try:
            prev = datetime.datetime.fromisoformat(prev_iso)
        except (ValueError, TypeError):
            prev = None
        if prev is not None and prev.tzinfo is None:
            prev = prev.replace(tzinfo=datetime.UTC)
    if prev is not None:
        same_day = to_local_time(prev, tz_name).date() == to_local_time(cur, tz_name).date()
        if same_day and (cur - prev) < _TIMESTAMP_GAP_THRESHOLD:
            return None
    local = to_local_time(cur, tz_name)
    return f"[{local.strftime('%A, %Y-%m-%d %I:%M %p').strip()}]"


def _with_marker(marker: str | None, text: str) -> str:
    """Prepend a timestamp *marker* to *text* on its own line, if present."""
    return f"{marker}\n{text}" if marker else text


def _stored_messages_to_agent_messages(
    messages: list[Any], tz_name: str = ""
) -> list[AgentMessage]:
    """Convert ``StoredMessage`` rows to typed ``AgentMessage`` objects.

    Mirrors the conversion logic of ``load_conversation_history`` so any
    caller operating on a custom slice (e.g. the admin compact-now path)
    sees the same shape the LLM would normally receive: tool interactions
    expanded, approval prompts and orphan approval replies dropped.

    Stateful across the loop because dropping an approval prompt also
    requires dropping the user's "yes/no" reply that immediately follows
    it. ``last_was_approval_prompt`` carries that flag forward.
    """
    history: list[AgentMessage] = []
    last_was_approval_prompt = False
    # Timestamp of the previous *visible* message (dropped approval prompts and
    # blank rows do not advance it), so gaps are measured between turns the LLM
    # actually sees.
    prev_iso = ""
    for msg in messages:
        if msg.direction == MessageDirection.OUTBOUND:
            # Feed the LLM its pre-receipt prose, not the dispatched body.
            # The dispatched body has the deterministic receipt block
            # appended by ``append_receipts``; reading it back here on the
            # next turn would train the model on its own appended
            # receipts, after which it reproduces the bullet on the next
            # write turn and the receipt grep has to clean it up
            # post-hoc. ``llm_reply_text`` is populated by
            # ``persist_outbound``; legacy rows (before migration 037)
            # have it empty and fall back to ``body``.
            content = msg.llm_reply_text or msg.body
        else:
            # Prefer processed context (includes media descriptions) over raw body
            content = msg.processed_context if msg.processed_context else msg.body
        if msg.direction == MessageDirection.INBOUND:
            # Rapid-fire attachment-only messages can be batched so the
            # placeholder row is persisted with no body and no processed
            # context. Keeping that blank row in history teaches the LLM
            # that the previous user turn was "silent", which can produce
            # stray clarification text on the next real message.
            if not (content or "").strip():
                last_was_approval_prompt = False
                continue
            # Drop the user's approval reply ("Yes", "Always", ...) when it
            # immediately follows a (now-filtered) approval prompt. Without
            # this, the orphan reply floats in history with no antecedent
            # and risks confusing the LLM. We only filter the strict
            # fast-path keyword set so a stray "Yes" in normal conversation
            # is preserved.
            if last_was_approval_prompt and _parse_approval_response(content) is not None:
                last_was_approval_prompt = False
                continue
            last_was_approval_prompt = False
            marker = _time_marker(prev_iso, msg.timestamp, tz_name)
            history.append(UserMessage(content=_with_marker(marker, content), seq=msg.seq))
            prev_iso = msg.timestamp
        else:
            # Check for stored tool interactions
            tool_interactions = _parse_tool_interactions(msg.tool_interactions_json)
            if tool_interactions:
                marker = _time_marker(prev_iso, msg.timestamp, tz_name)
                history.extend(
                    _expand_outbound_with_tools(
                        tool_interactions, _with_marker(marker, content), seq=msg.seq
                    )
                )
                last_was_approval_prompt = False
                prev_iso = msg.timestamp
            elif _is_approval_prompt(content):
                # Skip approval prompts (real ones persisted by older code,
                # plus any LLM-generated fake prompts that mimic the format).
                # Persisted prompts in past turns trained the LLM to produce
                # them as prose instead of calling tools, creating an
                # infinite-loop UX bug. Filtering at load time heals
                # already-poisoned sessions without a DB migration.
                last_was_approval_prompt = True
            else:
                marker = _time_marker(prev_iso, msg.timestamp, tz_name)
                history.append(AssistantMessage(content=_with_marker(marker, content), seq=msg.seq))
                last_was_approval_prompt = False
                prev_iso = msg.timestamp
    return history


async def load_conversation_history(
    session: SessionState,
    limit: int = DEFAULT_HISTORY_LIMIT,
    tz_name: str = "",
) -> list[AgentMessage]:
    """Load recent messages as typed message objects for LLM context.

    Returns a list of typed messages in chronological order, excluding the
    most recent (which is the current message being processed).

    *tz_name* is the user's IANA timezone, used to localize the timestamp
    markers prepended to messages separated by a significant time gap (see
    :func:`_time_marker`). Empty string falls back to UTC.

    For outbound messages that have ``tool_interactions_json``, the full
    tool call/result sequence is reconstructed so the LLM can see its
    prior tool usage.  Messages without tool interaction data are loaded
    as flat ``AssistantMessage``.

    The *limit* parameter is a soft safety net that bounds memory usage
    (default 500). Token-based trimming in the agent loop is the primary
    guard against exceeding the LLM context window.
    """
    all_messages = session.messages

    # Apply the trim watermark before any other filtering: messages with
    # ``seq <= last_trim_seq`` have been compacted out of LLM context and
    # their durable facts now live in MEMORY.md / USER.md / SOUL.md. Loading
    # them again would re-trigger trim and re-fire compaction every message.
    # NULL watermark = no filter (preserves pre-feature behavior).
    if session.last_trim_seq is not None:
        all_messages = [m for m in all_messages if m.seq > session.last_trim_seq]
    total_count = len(all_messages)

    # Get the most recent `limit` messages, excluding the current (last) one
    if total_count > 1:
        messages = all_messages[-(limit):][:-1] if total_count > limit else all_messages[:-1]
    else:
        messages = []

    history = _stored_messages_to_agent_messages(messages, tz_name=tz_name)
    logger.debug(
        "Loaded %d history messages for session %s",
        len(history),
        session.session_id,
    )
    return history


async def get_or_create_conversation(user_id: str) -> tuple[SessionState, bool]:
    """Get the user's conversation, creating it on first access.

    Each user has a single persistent conversation (enforced by the
    ``uq_sessions_user_id`` constraint). Returns ``(session, is_new)``
    where ``is_new`` is True only on the very first message for a user.
    """
    return await get_session_store(user_id).get_or_create_session()


class AdminCompactionResult(BaseModel):
    """Outcome of an admin-triggered context compaction.

    ``event_id`` is the row this call wrote and is non-null only when this
    call did real work. ``previous_event_id`` is populated only on no-op
    returns and points at the most recent prior ``CompactionEvent`` for
    the user (if any). The two together let an admin tell apart three
    states that previously all rendered as the same all-null no-op:

    * ``event_id != None``: this call did the work; ignore previous_event_id.
    * ``event_id is None`` and ``previous_event_id != None``: this call
      was a no-op because that prior event already advanced the
      watermark. Common when the admin retries within seconds of a
      successful first call.
    * Both ``None``: the user has never been compacted; nothing to do.
    """

    compacted_message_count: int
    new_watermark: int | None
    memory_updated: bool
    event_id: int | None
    previous_event_id: int | None = None


async def _most_recent_compaction_event_id(user_id: str) -> int | None:
    """Return the most recent ``CompactionEvent.id`` for ``user_id``, or None.

    Used by :func:`admin_compact_visible_messages` to disambiguate no-op
    returns; see ``AdminCompactionResult`` for the call-site rationale.
    Read-only, no side effects.
    """
    async with db_session_async() as db:
        return (
            await db.execute(
                select(CompactionEvent.id)
                .filter_by(user_id=user_id)
                .order_by(CompactionEvent.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()


async def admin_compact_visible_messages(
    user_id: str,
    keep_recent: int = 0,
    admin_note: str | None = None,
) -> AdminCompactionResult:
    """Synchronously compact every message currently visible to the agent.

    Companion to :func:`trigger_compaction_for_dropped`, but driven by an
    admin (not by the trim path) so the LLM-facing context can be reset
    when the conversation has been poisoned, e.g. by a now-fixed bug
    that taught the agent a wrong fact about its own capabilities.

    Resolves "everything visible" the same way ``load_conversation_history``
    does: messages with ``seq > sessions.last_trim_seq``, excluding orphan
    approval prompts/replies, with tool interactions expanded. The last
    *keep_recent* visible turns are preserved on the schema so the agent
    still has immediate context if the admin wants to surgically clear
    older noise without dropping a pending request.

    Phase 1 (synchronous, one transaction) inserts a pending
    ``CompactionEvent`` and advances ``sessions.last_trim_seq`` to the max
    seq of the to-compact set, mirroring ``trigger_compaction_for_dropped``
    so the next inbound's history loader already filters them.

    Phase 2 (synchronous, awaited) calls :func:`compact_session` with the
    optional admin note prepended to the conversation block. Unlike the
    trim-driven path, we await the LLM call here so the admin endpoint
    can return a real result and surface failures.
    """
    if keep_recent < 0:
        raise ValueError("keep_recent must be non-negative")

    if not settings.compaction_enabled:
        return AdminCompactionResult(
            compacted_message_count=0,
            new_watermark=None,
            memory_updated=False,
            event_id=None,
        )

    store = get_session_store(user_id)
    state, _ = await store.get_or_create_session()

    watermark = state.last_trim_seq or 0
    above = [m for m in state.messages if m.seq > watermark]
    if keep_recent > 0:
        to_compact_stored = above[:-keep_recent] if keep_recent <= len(above) else []
    else:
        to_compact_stored = list(above)

    if not to_compact_stored:
        return AdminCompactionResult(
            compacted_message_count=0,
            new_watermark=state.last_trim_seq,
            memory_updated=False,
            event_id=None,
            previous_event_id=await _most_recent_compaction_event_id(user_id),
        )

    agent_messages = _stored_messages_to_agent_messages(to_compact_stored)
    if not agent_messages:
        # All visible rows were filtered (e.g. only approval prompts).
        # Still advance the watermark so they stop reaching the LLM, but
        # skip the LLM call since there is nothing to extract facts from.
        max_seq = to_compact_stored[-1].seq
        await _advance_trim_watermark_only(user_id, max_seq)
        return AdminCompactionResult(
            compacted_message_count=0,
            new_watermark=max_seq,
            memory_updated=False,
            event_id=None,
            previous_event_id=await _most_recent_compaction_event_id(user_id),
        )

    # Mirror ``_seqs_from_dropped``: only ``UserMessage`` /
    # ``AssistantMessage`` carry ``seq``; tool-result and system messages
    # do not. ``getattr`` with isinstance narrows for the type checker.
    seqs = [getattr(m, "seq", None) for m in agent_messages]
    seqs = [s for s in seqs if isinstance(s, int)]
    if not seqs:
        return AdminCompactionResult(
            compacted_message_count=0,
            new_watermark=state.last_trim_seq,
            memory_updated=False,
            event_id=None,
            previous_event_id=await _most_recent_compaction_event_id(user_id),
        )
    min_seq = min(seqs)
    max_seq = max(seqs)
    triggered_at = datetime.datetime.now(datetime.UTC)

    event_id: int
    async with db_session_async() as db:
        event = CompactionEvent(
            user_id=user_id,
            triggered_at=triggered_at,
            status="pending",
            min_message_seq=min_seq,
            max_message_seq=max_seq,
            trimmed_count=len(agent_messages),
        )
        db.add(event)
        await db.flush()
        assert event.id is not None, "flush() must populate the autoincrement id"
        event_id = event.id

        cs = (await db.execute(select(ChatSession).filter_by(user_id=user_id))).scalar_one_or_none()
        if cs is not None:
            current = cs.last_trim_seq or 0
            if max_seq > current:
                cs.last_trim_seq = max_seq
        await db.commit()

    memory_update, _ = await compact_session(
        user_id,
        agent_messages,
        max_message_seq=max_seq,
        event_id=event_id,
        admin_note=admin_note,
    )

    return AdminCompactionResult(
        compacted_message_count=len(agent_messages),
        new_watermark=max_seq,
        memory_updated=bool(memory_update),
        event_id=event_id,
    )


async def _advance_trim_watermark_only(user_id: str, max_seq: int) -> None:
    """Advance ``sessions.last_trim_seq`` without inserting a compaction event.

    Used when an admin compaction would drop only filtered rows (e.g.
    nothing but approval prompts). There is nothing to extract facts
    from, so the LLM call is skipped, but the watermark still advances
    so the rows stop reaching the LLM.
    """
    async with db_session_async() as db:
        cs = (await db.execute(select(ChatSession).filter_by(user_id=user_id))).scalar_one_or_none()
        if cs is not None:
            current = cs.last_trim_seq or 0
            if max_seq > current:
                cs.last_trim_seq = max_seq
        await db.commit()
