"""Observer hook for LLM requests assembled by the agent loop.

Mirrors the module-level setter pattern used by ``set_pipeline_override``
in ``backend.app.agent.router``. Premium (or other plugins) can register
a single async callback that receives a versioned record of every LLM
request just before it is dispatched, for telemetry / token-efficiency
analysis purposes.

The observer runs inline with the LLM call but its exceptions are caught
and logged, so it never crashes the agent loop. Observers should return
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


@dataclass(frozen=True)
class LLMRequestPayload:
    """Versioned snapshot of an LLM request dispatched to observers.

    Observers should read fields by name and tolerate unknown ones via
    ``getattr(payload, "field", default)`` so newer OSS builds remain
    compatible with older observer implementations.
    """

    schema_version: int
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
    # Lowest persisted ``seq`` across user/assistant messages currently in
    # the prompt. Compaction trims older messages, which raises this value
    # on the next request -- observers use it as an "era marker" without
    # needing to query ``compaction_events`` themselves.
    min_message_seq_in_prompt: int | None
    started_at: datetime


LLMRequestObserver = Callable[[LLMRequestPayload], Awaitable[None]]

_observer: LLMRequestObserver | None = None


def set_llm_request_observer(observer: LLMRequestObserver | None) -> None:
    """Register the (single) observer that receives each LLM request.

    Pass ``None`` to clear. Premium calls this at module import time,
    similar to ``set_pipeline_override``.
    """
    global _observer
    _observer = observer


def get_llm_request_observer() -> LLMRequestObserver | None:
    """Return the registered observer, or ``None`` if none is set."""
    return _observer


def compute_min_message_seq(messages: list[AgentMessage]) -> int | None:
    """Return the lowest persisted ``seq`` across user/assistant messages.

    ``ToolResultMessage`` and ``SystemMessage`` are excluded -- only
    user/assistant turns carry an independent ``seq``. Returns ``None``
    when no message has a persisted seq (e.g. brand-new conversation
    with only in-memory entries).
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
    are logged and discarded so they never crash ``_call_llm_with_retry``.
    """
    observer = _observer
    if observer is None:
        return
    try:
        await observer(payload)
    except Exception:
        logger.exception("LLM request observer raised; continuing")
