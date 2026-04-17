"""Deterministic formatter for tool-call summaries appended to outbound messages.

Channels that do not render a separate tool-call UI (SMS/iMessage, Telegram)
receive a compact, deterministic summary so the user can see which tools
actually ran, independent of anything the LLM claims in its text. This is
the failsafe when the model hallucinates "I uploaded the photo" without
emitting the matching tool call.
"""

from __future__ import annotations

from backend.app.agent.context import StoredToolInteraction
from backend.app.agent.tools.base import ToolTags
from backend.app.agent.tools.names import ToolName

# Tools that are infrastructure, not user-visible actions. They get excluded
# from the summary because listing them adds noise without helping the user
# understand what was done to their data.
_HIDDEN_TOOL_NAMES: frozenset[str] = frozenset(
    {
        ToolName.LIST_CAPABILITIES,
    }
)

_SUMMARY_HEADER = "Tools used: "
_SUMMARY_SEPARATOR = "\n\n---\n"


def format_tool_call_summary(tool_calls: list[StoredToolInteraction]) -> str:
    """Return a single-line summary of tool calls, or empty string if none apply.

    The summary omits infrastructure tools (``list_capabilities``) and tools
    tagged ``SENDS_REPLY`` (those tools ARE the reply text, so listing them
    would be redundant). Failed calls are annotated with ``(failed)``.
    """
    visible: list[str] = []
    for tc in tool_calls:
        if tc.name in _HIDDEN_TOOL_NAMES:
            continue
        if ToolTags.SENDS_REPLY in tc.tags:
            continue
        label = tc.name
        if tc.is_error:
            label = f"{label} (failed)"
        visible.append(label)
    if not visible:
        return ""
    return _SUMMARY_HEADER + ", ".join(visible)


def append_tool_call_summary(reply_text: str, tool_calls: list[StoredToolInteraction]) -> str:
    """Append a tool-call summary to ``reply_text`` if any visible tools ran.

    Returns ``reply_text`` unchanged when there is nothing to summarize.
    """
    summary = format_tool_call_summary(tool_calls)
    if not summary:
        return reply_text
    if not reply_text:
        return summary
    return f"{reply_text}{_SUMMARY_SEPARATOR}{summary}"
