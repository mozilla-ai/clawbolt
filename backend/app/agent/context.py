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


async def load_conversation_history(
    session: SessionState,
    limit: int = DEFAULT_HISTORY_LIMIT,
) -> list[AgentMessage]:
    """Load recent messages as typed message objects for LLM context.

    Returns a list of typed messages in chronological order, excluding the
    most recent (which is the current message being processed).

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

    history: list[AgentMessage] = []
    tool_interaction_count = 0
    last_was_approval_prompt = False
    for msg in messages:
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
            history.append(UserMessage(content=content, seq=msg.seq))
        else:
            # Check for stored tool interactions
            tool_interactions = _parse_tool_interactions(msg.tool_interactions_json)
            if tool_interactions:
                tool_interaction_count += len(tool_interactions)
                history.extend(_expand_outbound_with_tools(tool_interactions, content, seq=msg.seq))
                last_was_approval_prompt = False
            elif _is_approval_prompt(content):
                # Skip approval prompts (real ones persisted by older code,
                # plus any LLM-generated fake prompts that mimic the format).
                # Persisted prompts in past turns trained the LLM to produce
                # them as prose instead of calling tools, creating an
                # infinite-loop UX bug. Filtering at load time heals
                # already-poisoned sessions without a DB migration.
                last_was_approval_prompt = True
            else:
                history.append(AssistantMessage(content=content, seq=msg.seq))
                last_was_approval_prompt = False
    logger.debug(
        "Loaded %d history messages (%d with tool interactions) for session %s",
        len(history),
        tool_interaction_count,
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
