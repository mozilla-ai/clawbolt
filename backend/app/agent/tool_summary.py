"""Deterministic receipt rendering for outbound replies.

Every outbound reply gets a compact receipt block appended for each
write-side tool that populated a ``ToolReceipt``. The receipt text is
generated from real API output by code, not by the LLM, so the user has
trustworthy evidence that the claimed action actually happened.

Read-side tools and tools that did not return a receipt contribute
nothing to the block. A message with no receipts produces no footer at
all.
"""

from __future__ import annotations

from backend.app.agent.context import StoredToolInteraction

_SUMMARY_SEPARATOR = "\n\n"

# Upper bound on the receipt block length. Past this, the tail collapses
# to ``+K more`` so a truly runaway tool count (dozens of actions in one
# turn) does not produce a runaway message. iMessage and the web chat
# have no hard length limit; this cap is mainly a safety valve for SMS
# (Linq) where each 160-char segment costs money.
_MAX_RECEIPTS_CHARS = 2000


def render_receipt_line(action: str, target: str, url: str | None) -> str:
    """Render one receipt as 1-2 plain-text lines.

    Used both when assembling the user-facing block and when echoing the
    rendered line back to the LLM inside the tool result (so the LLM knows
    exactly what will be shown and does not restate it).
    """
    head = f"- {action} {target}".rstrip()
    if url:
        return f"{head}\n  {url}"
    return head


def _collect_receipts(tool_calls: list[StoredToolInteraction]) -> list[str]:
    """Return rendered receipt lines for every successful tool call that
    populated a ``ToolReceipt``. Errors and read-side tools contribute
    nothing.
    """
    lines: list[str] = []
    for tc in tool_calls:
        if tc.is_error or tc.receipt is None:
            continue
        if not tc.receipt.action or not tc.receipt.target:
            continue
        lines.append(render_receipt_line(tc.receipt.action, tc.receipt.target, tc.receipt.url))
    return lines


def _truncate_block(lines: list[str]) -> str:
    """Join receipt lines, falling back to a ``+K more`` suffix when the
    block exceeds ``_MAX_RECEIPTS_CHARS``. The first receipts are kept
    intact so the most recent action is still legible.
    """
    full = "\n".join(lines)
    if len(full) <= _MAX_RECEIPTS_CHARS:
        return full
    kept: list[str] = []
    running = 0
    for idx, line in enumerate(lines):
        suffix = f"\n(+{len(lines) - idx} more)"
        addition = (1 if kept else 0) + len(line)
        if running + addition + len(suffix) > _MAX_RECEIPTS_CHARS:
            return "\n".join(kept) + f"\n(+{len(lines) - idx} more)"
        kept.append(line)
        running += addition
    return "\n".join(kept)


def format_receipts_block(tool_calls: list[StoredToolInteraction]) -> str:
    """Return the full receipt block or an empty string if nothing applies."""
    lines = _collect_receipts(tool_calls)
    if not lines:
        return ""
    return _truncate_block(lines)


def append_receipts(reply_text: str, tool_calls: list[StoredToolInteraction]) -> str:
    """Append a receipt block to ``reply_text`` if any write-side tool
    returned a receipt. Returns ``reply_text`` unchanged when there is
    nothing to confirm.
    """
    block = format_receipts_block(tool_calls)
    if not block:
        return reply_text
    if not reply_text:
        return block
    return f"{reply_text}{_SUMMARY_SEPARATOR}{block}"
