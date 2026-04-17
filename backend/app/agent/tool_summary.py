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

# Integration prefixes mapped to a user-facing label. Order matters only for
# readability; prefix matching is exact by the longest match (no overlap
# today, but keep the lookup deterministic).
_INTEGRATION_PREFIXES: tuple[tuple[str, str], ...] = (
    ("companycam_", "CompanyCam: "),
    ("quickbooks_", "QuickBooks: "),
    ("qb_", "QuickBooks: "),
    ("calendar_", "Calendar: "),
    ("gdrive_", "Google Drive: "),
    ("dropbox_", "Dropbox: "),
    ("telegram_", "Telegram: "),
)

_SUMMARY_HEADER = "Tools used: "
_SUMMARY_SEPARATOR = "\n\n---\n"

# Maximum characters of tool names to include. Past this we fall back to
# "first N, +K more" so the summary doesn't push an SMS past a multipart
# threshold. The cap is generous so small multi-tool sessions stay fully
# listed; the overflow path only kicks in for runaway tool counts.
_MAX_SUMMARY_CHARS = 240


def _prettify_tool_name(tool_name: str) -> str:
    """Return a user-facing label for a tool's internal name.

    Internal names are implementation identifiers like ``companycam_upload_photo``.
    The summary shows them to end users on plain-text channels, so we strip
    known integration prefixes and replace underscores with spaces.
    """
    for prefix, label in _INTEGRATION_PREFIXES:
        if tool_name.startswith(prefix):
            rest = tool_name[len(prefix) :].replace("_", " ")
            return f"{label}{rest}"
    return tool_name.replace("_", " ")


def _visible_labels(tool_calls: list[StoredToolInteraction]) -> list[str]:
    """Filter infrastructure/reply tools and return user-facing labels."""
    labels: list[str] = []
    for tc in tool_calls:
        if tc.name in _HIDDEN_TOOL_NAMES:
            continue
        if ToolTags.SENDS_REPLY in tc.tags:
            continue
        label = _prettify_tool_name(tc.name)
        if tc.is_error:
            label = f"{label} (failed)"
        labels.append(label)
    return labels


def _truncate_labels(labels: list[str]) -> str:
    """Join labels with commas, falling back to ``+K more`` if the result
    exceeds ``_MAX_SUMMARY_CHARS``. We keep as many labels as fit and
    append a count of the remainder so the user still gets an accurate
    picture of how many tools ran.
    """
    full = ", ".join(labels)
    if len(full) <= _MAX_SUMMARY_CHARS:
        return full
    kept: list[str] = []
    running = 0
    for idx, label in enumerate(labels):
        remaining_suffix = f" (+{len(labels) - idx} more)"
        addition = (2 if kept else 0) + len(label)
        if running + addition + len(remaining_suffix) > _MAX_SUMMARY_CHARS:
            return ", ".join(kept) + f" (+{len(labels) - idx} more)"
        kept.append(label)
        running += addition
    return ", ".join(kept)


def format_tool_call_summary(tool_calls: list[StoredToolInteraction]) -> str:
    """Return a single-line summary of tool calls, or empty string if none apply.

    The summary omits infrastructure tools (``list_capabilities``) and tools
    tagged ``SENDS_REPLY`` (those tools ARE the reply text, so listing them
    would be redundant). Failed calls are annotated with ``(failed)``. When
    the label list would exceed ``_MAX_SUMMARY_CHARS``, the tail is replaced
    with ``(+K more)`` to stay SMS-friendly.
    """
    labels = _visible_labels(tool_calls)
    if not labels:
        return ""
    return _SUMMARY_HEADER + _truncate_labels(labels)


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
