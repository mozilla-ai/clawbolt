"""Typed message dataclasses for the agent loop.

These replace raw ``dict[str, Any]`` messages inside the agent, providing
type safety and eliminating fragile ``.get()`` chains.  Dict serialization
happens only at the LLM API boundary via ``to_dict()``.

Serialization targets the Anthropic Messages API format used by ``amessages``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolCallRequest:
    """A single tool invocation requested by the LLM."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class SystemMessage:
    """System prompt message.

    Not serialized into the messages list; passed as the ``system``
    parameter to ``amessages`` instead.
    """

    content: str


@dataclass(frozen=True)
class UserMessage:
    """User (user) message."""

    content: str

    def to_dict(self) -> dict[str, Any]:
        return {"role": "user", "content": self.content}


@dataclass(frozen=True)
class AssistantMessage:
    """Assistant response, optionally containing tool calls."""

    content: str | None = None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        blocks: list[dict[str, Any]] = []
        if self.content:
            blocks.append({"type": "text", "text": self.content})
        for tc in self.tool_calls:
            blocks.append(
                {
                    "type": "tool_use",
                    "id": tc.id,
                    "name": tc.name,
                    "input": tc.arguments,
                }
            )
        return {"role": "assistant", "content": blocks}


@dataclass(frozen=True)
class ToolResultMessage:
    """Result of a tool execution, sent back to the LLM."""

    tool_call_id: str
    content: str
    is_error: bool = False

    def to_content_block(self) -> dict[str, Any]:
        """Return a single ``tool_result`` content block."""
        block: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": self.tool_call_id,
            "content": self.content,
        }
        if self.is_error:
            block["is_error"] = True
        return block


# Union of all message types the agent loop works with.
AgentMessage = SystemMessage | UserMessage | AssistantMessage | ToolResultMessage


def messages_to_messages_api(
    messages: list[AgentMessage],
) -> tuple[str | None, list[dict[str, Any]]]:
    """Convert typed messages to Anthropic Messages API format.

    Returns ``(system_prompt, messages_list)`` where *system_prompt* is
    extracted from the first ``SystemMessage`` (if present) and
    *messages_list* contains the remaining messages serialized for the
    ``amessages`` ``messages`` parameter.

    Consecutive ``ToolResultMessage`` objects are merged into a single
    ``user`` message with multiple ``tool_result`` content blocks, as
    required by the Anthropic API.
    """
    system: str | None = None
    result: list[dict[str, Any]] = []
    pending_tool_results: list[dict[str, Any]] = []

    def _flush_tool_results() -> None:
        if pending_tool_results:
            result.append({"role": "user", "content": list(pending_tool_results)})
            pending_tool_results.clear()

    for m in messages:
        if isinstance(m, SystemMessage):
            system = m.content
            continue

        if isinstance(m, ToolResultMessage):
            pending_tool_results.append(m.to_content_block())
            continue

        # Non-tool-result message: flush any pending results first
        _flush_tool_results()

        if isinstance(m, (UserMessage, AssistantMessage)):
            result.append(m.to_dict())

    # Flush any trailing tool results
    _flush_tool_results()

    return system, result
