"""Message trimming utilities for the agent loop.

Provides deterministic summarization of dropped messages and block-aware
trimming that preserves tool-call / tool-result pairing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from backend.app.agent.messages import (
    AgentMessage,
    AssistantMessage,
    SystemMessage,
    ToolResultMessage,
    UserMessage,
)
from backend.app.config import settings

_SUMMARY_MAX_CHARS = 500

CONTEXT_TRIM_TARGET_TOKENS = settings.context_trim_target_tokens

_OVERHEAD_TOKEN_ESTIMATE = 10_000
_CHARS_PER_TOKEN = 4


@dataclass
class TrimResult:
    """Result of trimming conversation messages."""

    messages: list[AgentMessage]
    dropped: list[AgentMessage] = field(default_factory=list)


def summarize_dropped_messages(dropped: list[AgentMessage]) -> str:
    """Build a deterministic summary of messages that were trimmed from context.

    Extracts message count, tool calls made, and key topics (first line of
    each user/assistant message). Fast and deterministic: no LLM call needed.
    """
    user_snippets: list[str] = []
    assistant_snippets: list[str] = []
    tool_calls_made: list[str] = []

    for msg in dropped:
        if isinstance(msg, UserMessage) and msg.content:
            first_line = msg.content.split("\n", 1)[0][:80]
            user_snippets.append(first_line)
        elif isinstance(msg, AssistantMessage):
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    tool_calls_made.append(tc.name)
            if msg.content:
                first_line = msg.content.split("\n", 1)[0][:80]
                assistant_snippets.append(first_line)
        # ToolResultMessages are covered by the tool_calls_made list

    parts: list[str] = [f"{len(dropped)} earlier message(s) were trimmed from context."]

    if user_snippets:
        topics = "; ".join(user_snippets[:5])
        if len(user_snippets) > 5:
            topics += f" (and {len(user_snippets) - 5} more)"
        parts.append(f"User topics: {topics}")

    if assistant_snippets:
        topics = "; ".join(assistant_snippets[:3])
        parts.append(f"Assistant discussed: {topics}")

    if tool_calls_made:
        unique_tools = sorted(set(tool_calls_made))
        parts.append(f"Tools used: {', '.join(unique_tools)}")

    summary = " ".join(parts)
    return summary[:_SUMMARY_MAX_CHARS]


def _content_length(msgs: list[AgentMessage]) -> int:
    """Return total character count (used only for proportional scaling)."""
    total = 0
    for m in msgs:
        if isinstance(m, (SystemMessage, UserMessage)):
            total += len(m.content or "")
        elif isinstance(m, AssistantMessage):
            total += len(m.content or "")
            for tc in m.tool_calls:
                total += len(tc.name) + len(str(tc.arguments))
        elif isinstance(m, ToolResultMessage):
            total += len(m.content or "")
    return total


def trim_messages(
    messages: list[AgentMessage],
    target_tokens: int = CONTEXT_TRIM_TARGET_TOKENS,
    input_tokens: int | None = None,
) -> TrimResult:
    """Trim conversation messages to fit within a token budget.

    Uses *input_tokens* (from ``response.usage.input_tokens``) to make
    accurate trimming decisions using the API-reported token count. When
    *input_tokens* is ``None`` (e.g. first call in a session), a
    character-based heuristic (~4 chars/token + overhead) is used to
    estimate whether trimming is needed.

    Keeps the system prompt (first message) and removes the oldest
    conversation messages until the content fits within *target_tokens*.
    Tool-call / tool-result pairs are treated as atomic units: an
    ``AssistantMessage`` with ``tool_calls`` is never removed without also
    removing the ``ToolResultMessage`` entries that follow it (and
    vice-versa).

    Dropped messages are summarized and injected as a context note so
    the LLM retains awareness of what was discussed.

    Returns a ``TrimResult`` containing the (possibly trimmed) message
    list and the list of dropped messages.
    """
    if len(messages) <= 2:
        return TrimResult(messages=messages)

    actual_input_tokens: int
    if input_tokens is not None:
        actual_input_tokens = input_tokens
    else:
        # Estimate tokens from character count for first-call-in-session
        actual_input_tokens = (
            _content_length(messages) // _CHARS_PER_TOKEN + _OVERHEAD_TOKEN_ESTIMATE
        )

    def _tokens_for(msgs: list[AgentMessage]) -> int:
        """Scale the known input_tokens by the content-length ratio."""
        orig_len = _content_length(messages) or 1
        return int(actual_input_tokens * _content_length(msgs) / orig_len)

    if _tokens_for(messages) <= target_tokens:
        return TrimResult(messages=messages)

    system = messages[0]
    body = list(messages[1:])

    # Group the body into "blocks" that must be removed together.
    blocks: list[list[AgentMessage]] = []
    i = 0
    while i < len(body):
        msg = body[i]
        if isinstance(msg, AssistantMessage) and msg.tool_calls:
            block: list[AgentMessage] = [msg]
            j = i + 1
            while j < len(body):
                if isinstance(body[j], ToolResultMessage):
                    block.append(body[j])
                    j += 1
                else:
                    break
            blocks.append(block)
            i = j
        else:
            blocks.append([msg])
            i += 1

    # Remove blocks from the front (oldest) until we fit the budget,
    # but always keep at least the last block.
    dropped: list[AgentMessage] = []
    while len(blocks) > 1:
        remaining: list[AgentMessage] = [system]
        for blk in blocks:
            remaining.extend(blk)
        if _tokens_for(remaining) <= target_tokens:
            break
        removed_block = blocks.pop(0)
        dropped.extend(removed_block)

    result: list[AgentMessage] = [system]
    if dropped:
        summary = summarize_dropped_messages(dropped)
        result.append(UserMessage(content=f"[Summary of earlier conversation: {summary}]"))
    for blk in blocks:
        result.extend(blk)
    return TrimResult(messages=result, dropped=dropped)
