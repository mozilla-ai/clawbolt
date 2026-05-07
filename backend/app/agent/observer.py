"""Observer hook for LLM requests dispatched by the agent layer.

Mirrors the module-level setter pattern used by ``set_pipeline_override``
in ``backend.app.agent.router``. Premium (or other plugins) can register
a single async callback that receives a versioned snapshot of every LLM
request the agent layer dispatches (main agent loop, compaction,
heartbeat decision), for telemetry and token-efficiency analysis.

The observer runs inline with the LLM call but its exceptions are caught
and logged, so it never crashes the caller. Observers should return
quickly (e.g. by enqueueing work onto a background task) to avoid adding
latency to the user-facing response.
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from backend.app.agent.messages import (
    AgentMessage,
    AssistantMessage,
    UserMessage,
)

logger = logging.getLogger(__name__)


# Stable strings for ``LLMRequestPayload.purpose``. Match
# ``LLMUsageLog.purpose`` values so observers can correlate against the
# usage-log table without translation.
PURPOSE_AGENT_MAIN = "agent_main"
PURPOSE_AGENT_FOLLOWUP = "agent_followup"  # post-trim retry of the main agent call
PURPOSE_COMPACTION = "compaction"
PURPOSE_HEARTBEAT_DECISION = "heartbeat_decision"


@dataclass(frozen=True)
class LLMRequestPayload:
    """Versioned snapshot of an LLM request dispatched to observers.

    Observers should read fields by name and tolerate unknown ones via
    ``getattr(payload, "field", default)`` so newer OSS builds remain
    compatible with older observer implementations.

    The dataclass is ``frozen=True`` to prevent field rebinding, but the
    ``messages`` and ``tools`` lists ARE THE SAME OBJECTS that get passed
    into ``amessages`` immediately after the observer returns. Observers
    must not mutate them or they will corrupt the in-flight LLM call.
    Treat the payload as read-only; if an observer needs a mutable copy,
    take a deep copy itself.

    ``min_message_seq_in_prompt`` is the era marker for agent-loop calls:
    the lowest persisted ``seq`` across user/assistant messages currently
    in the prompt. Compaction trims older messages, which raises this
    value on the next request, so observers can detect compaction-driven
    trimming without querying ``compaction_events``. Synthetic messages
    (e.g. summary placeholders injected by ``trim_messages``) are not
    persisted and have ``seq=None``; they are intentionally skipped, so
    the field reflects "lowest persisted seq still visible to the model"
    rather than a complete inventory of the prompt. For non-agent-loop
    purposes (compaction, heartbeat) the value is ``None``.
    """

    schema_version: int
    purpose: str
    user_id: str
    session_id: str | None
    request_id: str | None
    model: str
    provider: str
    max_tokens: int
    thinking: dict[str, Any] | None
    system: str | list[dict[str, Any]] | None
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] | None
    min_message_seq_in_prompt: int | None
    started_at: datetime


LLMRequestObserver = Callable[[LLMRequestPayload], Awaitable[None]]

_observer: LLMRequestObserver | None = None


def set_llm_request_observer(observer: LLMRequestObserver | None) -> None:
    """Register the (single) observer that receives each LLM request.

    Pass ``None`` to clear. Premium calls this at module import time,
    similar to ``set_pipeline_override``.

    The observer must be safe to invoke concurrently from multiple agent
    loops (one per active user) and from the compaction / heartbeat
    schedulers. The OSS side does no locking around the dispatch.
    """
    global _observer
    _observer = observer


def get_llm_request_observer() -> LLMRequestObserver | None:
    """Return the registered observer, or ``None`` if none is set."""
    return _observer


def compute_min_message_seq(messages: list[AgentMessage]) -> int | None:
    """Return the lowest persisted ``seq`` across user/assistant messages.

    Used by the main agent loop to populate the era-marker field on
    ``LLMRequestPayload``. ``ToolResultMessage`` and ``SystemMessage`` are
    excluded -- only user/assistant turns carry an independent ``seq``.
    Synthetic messages with ``seq=None`` (the live inbound, summary
    placeholders from ``trim_messages``) are intentionally skipped: they
    are not persisted and would otherwise mask era boundaries.

    Returns ``None`` when no persisted message remains in the prompt
    (e.g. brand-new conversation with only the live inbound).
    """
    seqs = [
        m.seq
        for m in messages
        if isinstance(m, (UserMessage, AssistantMessage)) and m.seq is not None
    ]
    return min(seqs) if seqs else None


async def emit_llm_request(payload: LLMRequestPayload) -> None:
    """Dispatch ``payload`` to the registered observer, swallowing errors.

    No-op when no observer is registered. Errors raised by the observer
    are logged and discarded so they never crash the caller. The observer
    remains registered after a failure: a transient issue does not
    deregister it.
    """
    observer = _observer
    if observer is None:
        return
    try:
        await observer(payload)
    except Exception:
        logger.exception("LLM request observer raised; payload dropped")
