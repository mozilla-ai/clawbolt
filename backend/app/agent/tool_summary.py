"""Deterministic receipt rendering for outbound replies.

Every outbound reply gets a compact receipt block appended for each
write-side tool that populated a ``ToolReceipt``. The receipt text is
generated from real API output by code, not by the LLM, so the user has
trustworthy evidence that the claimed action actually happened.

Read-side tools and tools that did not return a receipt contribute
nothing to the block. A message with no receipts produces no footer at
all.

When a single turn produces multiple receipts that share the same URL
(e.g. create project + upload photo + tag + archive all on the same
project URL), the block collapses them into one entry with a joined
verb list, so the iMessage footer stays compact on multi-action turns.
"""

from __future__ import annotations

import re

from backend.app.agent.context import StoredToolInteraction

# Last line of defence: any integration that builds a ToolReceipt must
# not let newlines, tabs, or control chars reach the rendered output.
# Integrations should sanitize earlier (see
# ``backend/app/agent/tools/companycam_receipts._sanitize``), but the
# renderer strips again so a new integration that forgets still ships
# safe output.
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _scrub(text: str) -> str:
    """Collapse control chars to a single space and trim the result."""
    if not text:
        return text
    return re.sub(r"\s+", " ", _CTRL_RE.sub(" ", text)).strip()


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
    exactly what will be shown and does not restate it). Action and target
    are scrubbed of control characters so a rogue newline cannot forge a
    fake receipt line in the output.
    """
    head = f"- {_scrub(action)} {_scrub(target)}".rstrip()
    if url:
        return f"{head}\n  {_scrub(url)}"
    return head


# Verb-reduction patterns used when grouping receipts that share a URL.
# Drop the known noun suffixes so a list of actions reads as verbs:
#   "Created CompanyCam project"          → "created"
#   "Archived CompanyCam project"         → "archived"
#   "Commented on CompanyCam project"     → "commented"
#   "Uploaded photo to CompanyCam"        → "uploaded photo to"
# Longest patterns first so we match the most specific form.
_VERB_SUFFIXES: tuple[str, ...] = (
    " on companycam project",
    " on companycam photo",
    " on companycam",
    " companycam project",
    " companycam photo",
    " companycam checklist",
    " companycam",
)


def _verb_phrase(action: str) -> str:
    """Reduce an action string to the bare verb for a grouped receipt."""
    lower = action.lower()
    for suffix in _VERB_SUFFIXES:
        if lower.endswith(suffix):
            lower = lower[: -len(suffix)]
            break
    return lower.strip()


def _render_group(
    entries: list[tuple[str, str]],
    target: str,
    url: str,
) -> str:
    """Render a set of same-URL receipts as one three-line block.

    ``entries`` is a list of (action, target) pairs, ordered by their
    original appearance. The final target wins (most informative: it's
    the entity's state after the last action). Each entry contributes a
    verb phrase to the joined second line, with a parenthesised target
    when the per-entry target differs from the final target.
    """
    verbs: list[str] = []
    for action, entry_target in entries:
        verb = _verb_phrase(action)
        clean_entry_target = _scrub(entry_target)
        if (
            clean_entry_target
            and clean_entry_target != target
            and clean_entry_target not in ("photo", "project")
        ):
            verb = f"{verb} ({clean_entry_target})"
        if verb:
            verbs.append(verb)

    head = f"- {_scrub(target)}".rstrip()
    clean_url = _scrub(url)
    if not verbs:
        return f"{head}\n  {clean_url}"
    return f"{head}\n  {' · '.join(verbs)}\n  {clean_url}"


class _Bucket:
    """One URL-keyed group of receipts. ``url=None`` means ungroupable."""

    __slots__ = ("entries", "url")

    def __init__(self, url: str | None, entries: list[tuple[str, str]]) -> None:
        self.url = url
        self.entries = entries


def _collect_receipts(tool_calls: list[StoredToolInteraction]) -> list[str]:
    """Return rendered receipt lines for every successful tool call that
    populated a ``ToolReceipt``. Errors and read-side tools contribute
    nothing.

    Receipts that share the same URL are grouped into a single block so
    multi-action turns stay iMessage-compact. Receipts without a URL
    each render as their own line (no grouping possible since the URL
    is the grouping key).
    """
    buckets: list[_Bucket] = []
    by_url: dict[str, int] = {}
    for tc in tool_calls:
        if tc.is_error or tc.receipt is None:
            continue
        if not tc.receipt.action or not tc.receipt.target:
            continue

        url = tc.receipt.url
        action = tc.receipt.action
        target = tc.receipt.target

        if not url:
            # Non-groupable: ship as its own entry.
            buckets.append(_Bucket(url=None, entries=[(action, target)]))
            continue

        if url in by_url:
            buckets[by_url[url]].entries.append((action, target))
        else:
            by_url[url] = len(buckets)
            buckets.append(_Bucket(url=url, entries=[(action, target)]))

    lines: list[str] = []
    for bucket in buckets:
        entries = bucket.entries
        url = bucket.url
        if url is None:
            action, target = entries[0]
            lines.append(render_receipt_line(action, target, None))
            continue
        if len(entries) == 1:
            action, target = entries[0]
            lines.append(render_receipt_line(action, target, url))
        else:
            final_target = entries[-1][1]
            lines.append(_render_group(entries, final_target, url))

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
