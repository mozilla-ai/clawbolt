"""Typed event dataclasses for the agent lifecycle.

Emitted at key points during agent processing so that subscribers can
observe progress, collect metrics, or implement streaming without
modifying the core loop.  When no subscribers are registered, event
emission is effectively a no-op (just dataclass construction).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class AgentStartEvent:
    """Emitted when the agent begins processing a message."""

    user_id: str
    message_context: str


@dataclass(frozen=True)
class TurnStartEvent:
    """Emitted at the start of each LLM call round."""

    round_number: int
    message_count: int


@dataclass(frozen=True)
class ToolExecutionStartEvent:
    """Emitted before a tool function is called."""

    tool_name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolExecutionEndEvent:
    """Emitted after a tool function completes."""

    tool_name: str
    result: str
    is_error: bool
    duration_ms: float


@dataclass(frozen=True)
class TurnEndEvent:
    """Emitted at the end of each LLM call round."""

    round_number: int
    has_more_tool_calls: bool


@dataclass(frozen=True)
class AgentEndEvent:
    """Emitted when the agent finishes processing."""

    reply_text: str
    actions_taken: list[str] = field(default_factory=list)
    total_duration_ms: float = 0.0


# Union type for all events
AgentEvent = (
    AgentStartEvent
    | TurnStartEvent
    | ToolExecutionStartEvent
    | ToolExecutionEndEvent
    | TurnEndEvent
    | AgentEndEvent
)
