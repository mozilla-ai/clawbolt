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
CONTEXT_TRIM_TARGET_TURNS = settings.context_trim_target_turns
CONTEXT_TRIM_TRIGGER_TURNS = settings.context_trim_trigger_turns

_OVERHEAD_TOKEN_ESTIMATE = 10_000
_CHARS_PER_TOKEN = 4

# When ``trigger_turns`` is unset and ``target_turns`` is set, the trigger
# defaults to ``target_turns + _DEFAULT_TRIGGER_BUFFER_TURNS``. Hysteresis
# prevents per-message re-triggering once the conversation crosses the cap.
_DEFAULT_TRIGGER_BUFFER_TURNS = 16


def _count_user_turns(msgs: list[AgentMessage]) -> int:
    """Count user-authored turns in *msgs*.

    Used as the unit for the turn-count cap. The system prompt and
    summary placeholders are not user turns; only the original
    ``UserMessage`` objects from the conversation count.
    """
    return sum(1 for m in msgs if isinstance(m, UserMessage))


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
    target_turns: int | None = CONTEXT_TRIM_TARGET_TURNS,
    trigger_turns: int | None = CONTEXT_TRIM_TRIGGER_TURNS,
    trigger_tokens: int | None = None,
    input_tokens: int | None = None,
) -> TrimResult:
    """Trim conversation messages to fit within a token and turn budget.

    Uses *input_tokens* (from ``response.usage.input_tokens``) to make
    accurate trimming decisions using the API-reported token count. When
    *input_tokens* is ``None`` (e.g. first call in a session), a
    character-based heuristic (~4 chars/token + overhead) is used to
    estimate whether trimming is needed.

    Keeps the system prompt (first message) and removes the oldest
    conversation messages until the content fits within *target_tokens*
    and the number of remaining user turns is within *target_turns*.

    The token budget is the primary governor. Trim fires when the
    estimated token count exceeds *trigger_tokens* and drops the oldest
    turns until the count is back within *target_tokens*, leaving
    ``trigger - target`` tokens of headroom before the next trim. This
    hysteresis prevents per-message re-triggering: a single threshold
    (target == trigger) leaves the resting state exactly at the ceiling,
    so the next message re-fires trim plus the downstream compaction LLM
    call. When *trigger_tokens* is ``None``, it defaults to *target_tokens*
    (no token-side hysteresis), so callers that want hysteresis must pass
    ``context_trim_trigger_tokens`` explicitly.

    The turn cap is a backstop with the same hysteresis: it fires when the
    user-turn count exceeds *trigger_turns* and drops to *target_turns*.
    When *trigger_turns* is ``None``, it defaults to ``target_turns + 16``.
    When *target_turns* is ``None``, the turn-count guard is disabled
    entirely (token budget only).

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

    # Resolve the trigger thresholds. Hysteresis = trigger - target.
    # Token trigger defaults to the target (no hysteresis) when unset.
    effective_trigger_tokens = trigger_tokens if trigger_tokens is not None else target_tokens

    effective_trigger_turns: int | None
    if trigger_turns is not None:
        effective_trigger_turns = trigger_turns
    elif target_turns is not None:
        effective_trigger_turns = target_turns + _DEFAULT_TRIGGER_BUFFER_TURNS
    else:
        effective_trigger_turns = None

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

    over_token_budget = _tokens_for(messages) > effective_trigger_tokens
    over_turn_budget = (
        effective_trigger_turns is not None
        and _count_user_turns(messages) > effective_trigger_turns
    )
    if not over_token_budget and not over_turn_budget:
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
            # History rebuild expands one outbound DB row into the
            # tool-call AssistantMessage, its ToolResultMessages, AND a
            # final-reply AssistantMessage, all sharing the row's seq
            # (see context._expand_outbound_with_tools). Treat that
            # reply as part of this block: dropping the tool-call half
            # while keeping the reply would advance the trim watermark
            # over the shared seq and silently filter the kept reply
            # from the next turn's history (issue #1433). Live-loop
            # messages carry seq=None and are unaffected.
            if j < len(body):
                nxt = body[j]
                if (
                    isinstance(nxt, AssistantMessage)
                    and not nxt.tool_calls
                    and msg.seq is not None
                    and nxt.seq == msg.seq
                ):
                    block.append(nxt)
                    j += 1
            blocks.append(block)
            i = j
        else:
            blocks.append([msg])
            i += 1

    def _fits(remaining: list[AgentMessage]) -> bool:
        if _tokens_for(remaining) > target_tokens:
            return False
        return not (target_turns is not None and _count_user_turns(remaining) > target_turns)

    # Remove blocks from the front (oldest) until both budgets are
    # satisfied, but always keep at least the last block.
    dropped: list[AgentMessage] = []
    while len(blocks) > 1:
        remaining: list[AgentMessage] = [system]
        for blk in blocks:
            remaining.extend(blk)
        if _fits(remaining):
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
