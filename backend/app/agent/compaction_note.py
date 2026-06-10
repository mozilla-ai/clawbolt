"""Continuity note for the window between trim and compaction completion.

``trigger_compaction_for_dropped`` advances the trim watermark
synchronously, so the dropped rows vanish from LLM context on the very
next message, but the compaction LLM call that extracts their facts into
MEMORY.md runs asynchronously and may still be in flight (or may have
failed). In that window the agent has amnesia for the dropped range, at
the worst possible moment: immediately after the trim, when the dropped
content is most likely still topical (issue #1432).

This module covers the gap without touching the watermark semantics:
while the user has a *recent* ``'pending'`` compaction event, a terse
deterministic summary of the covered rows (the same
``summarize_dropped_messages`` shape the trim turn itself saw) is
rebuilt from the durable message rows and injected as a dynamic
system-prompt section. Once the event flips to ``'completed'``, the note
disappears and MEMORY.md / HISTORY.md carry the facts.

The summary is recomputed per turn rather than persisted: the inputs are
durable (event seq range + message rows are never deleted), the
summarizer is deterministic and cheap (no LLM), and recomputing avoids a
schema migration. The common case (no pending events) costs one indexed
SELECT per turn.

The note is bounded in time: only events triggered in the last
``_NOTE_WINDOW_MINUTES`` qualify, so an event that stays ``'pending'``
forever (crashed compaction, retries exhausted) does not pin a stale
note into every prompt indefinitely. Permanent failures are the retry
sweep's domain (issue #1431), not this note's.
"""

from __future__ import annotations

import datetime
import logging

from sqlalchemy import Select, select

from backend.app.agent.messages import AgentMessage, AssistantMessage, UserMessage
from backend.app.agent.trimming import summarize_dropped_messages
from backend.app.database import AsyncSessionLocal
from backend.app.enums import MessageDirection
from backend.app.models import ChatSession, CompactionEvent, Message

logger = logging.getLogger(__name__)

# Only events this recent produce a note. The gap being covered is
# seconds-to-minutes (the async LLM call); anything older is either
# completed (no note needed) or stuck (the retry sweep's problem).
_NOTE_WINDOW_MINUTES = 60

# Hard cap on rows loaded per note rebuild, summed across the user's
# qualifying events. The summarizer reads first lines only and its
# output is capped at 500 chars, so deep ranges add nothing.
_MAX_NOTE_ROWS = 400


def _recent_pending_events_select(
    user_id: str,
    cutoff_utc: datetime.datetime,
) -> Select[tuple[CompactionEvent]]:
    """Pending events recent enough to still be 'in flight' for this user."""
    return (
        select(CompactionEvent)
        .where(
            CompactionEvent.user_id == user_id,
            CompactionEvent.status == "pending",
            CompactionEvent.triggered_at >= cutoff_utc,
            CompactionEvent.min_message_seq.is_not(None),
            CompactionEvent.max_message_seq.is_not(None),
        )
        .order_by(CompactionEvent.min_message_seq.asc())
    )


def _rows_to_agent_messages(rows: list[Message]) -> list[AgentMessage]:
    """Minimal Message-row conversion for the deterministic summarizer.

    Deliberately simpler than ``context._stored_messages_to_agent_messages``
    (which this module cannot import without a cycle through
    ``system_prompt``): no tool expansion, no approval-prompt filtering.
    The summarizer only reads first lines of user/assistant content, so
    the simplified shape produces the same topics line.
    """
    out: list[AgentMessage] = []
    for msg in rows:
        if msg.direction == MessageDirection.INBOUND:
            content = msg.processed_context or msg.body or ""
            if content.strip():
                out.append(UserMessage(content=content))
        else:
            content = msg.llm_reply_text or msg.body or ""
            if content.strip():
                out.append(AssistantMessage(content=content))
    return out


async def build_pending_compaction_note(user_id: str) -> str:
    """Summary of rows covered by the user's in-flight compaction events.

    Returns ``""`` when the user has no recent pending events (the common
    case, one indexed SELECT). Failures are swallowed: a broken note must
    never block the message turn it decorates.
    """
    try:
        cutoff = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
            minutes=_NOTE_WINDOW_MINUTES
        )
        db = AsyncSessionLocal()
        try:
            events = list(
                (await db.execute(_recent_pending_events_select(user_id, cutoff))).scalars().all()
            )
            if not events:
                return ""

            cs = (
                await db.execute(select(ChatSession).filter_by(user_id=user_id))
            ).scalar_one_or_none()
            if cs is None:
                return ""

            rows: list[Message] = []
            budget = _MAX_NOTE_ROWS
            for event in events:
                if budget <= 0:
                    break
                event_rows = list(
                    (
                        await db.execute(
                            select(Message)
                            .where(
                                Message.session_id == cs.id,
                                Message.seq >= event.min_message_seq,
                                Message.seq <= event.max_message_seq,
                            )
                            .order_by(Message.seq)
                            .limit(budget)
                        )
                    )
                    .scalars()
                    .all()
                )
                rows.extend(event_rows)
                budget -= len(event_rows)
        finally:
            await db.close()

        agent_messages = _rows_to_agent_messages(rows)
        if not agent_messages:
            return ""
        summary = summarize_dropped_messages(agent_messages)
        return (
            "These messages were just archived from your context; their durable "
            "facts are being written to your memory right now and will appear "
            "in the Your Memory section shortly.\n" + summary
        )
    except Exception:
        logger.exception("Failed to build pending-compaction note for user %s", user_id)
        return ""
