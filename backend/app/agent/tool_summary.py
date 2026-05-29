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
# ``backend/app/integrations/companycam/receipts._sanitize``), but the
# renderer strips again so a new integration that forgets still ships
# safe output.
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _scrub(text: str) -> str:
    """Collapse control chars to a single space and trim the result."""
    if not text:
        return text
    return re.sub(r"\s+", " ", _CTRL_RE.sub(" ", text)).strip()


def _display_url(url: str) -> str:
    """Render a deep link in compact form for plain-text channels.

    Strips the ``https://`` scheme so auto-linking channels (iMessage,
    Telegram, webchat) still render a tappable link while the visible text
    is eight characters shorter. Bare or non-https URLs pass through so
    an unusual scheme stays visible as a signal.

    Exception: keep the scheme when the URL embeds another URL in its query
    string (a literal ``://`` or a percent-encoded ``%2F%2F``), e.g. an OAuth
    authorize link carrying ``redirect_uri=https%3A%2F%2F...`` and
    ``scope=https%3A%2F%2F...``. Without the leading scheme, iMessage's data
    detector falls back to loose domain detection, latches onto the embedded
    domain (``clawbolt.ai`` inside the redirect_uri) partway through the query,
    and splits the link there. The user taps a truncated URL missing every
    param after the embedded domain, including ``response_type`` -- Google then
    rejects it with "Required parameter is missing: response_type". A schemed
    URL is detected as one contiguous token up to the next whitespace, so the
    whole link stays tappable. The 8-char saving is not worth a broken link.

    Plain deep links without an embedded URL (CompanyCam ``companycam.com/p/x``,
    QuickBooks ``app.qbo.intuit.com/app/invoice?txnId=4782``) have no second
    domain to latch onto, so they keep the compact stripped form.
    """
    if not url.startswith("https://"):
        return url
    lowered = url.lower()
    # Two markers of an embedded URL: a second literal scheme separator, or a
    # percent-encoded one in a query value. ``%3a%2f%2f`` is a subset of
    # ``%2f%2f``, so the latter check covers both encodings.
    if lowered.count("://") > 1 or "%2f%2f" in lowered:
        return url
    return url.removeprefix("https://")


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
        return f"{head}\n  {_scrub(_display_url(url))}"
    return head


# Verb-reduction used when grouping receipts that share a URL. Strip
# any "companycam [noun]" tail from the action so it reads as a verb:
#   "Created CompanyCam project"          → "created"
#   "Archived CompanyCam project"         → "archived"
#   "Commented on CompanyCam project"     → "commented"
#   "Uploaded photo to CompanyCam"        → "uploaded photo to"
#   "Tagged CompanyCam photo"             → "tagged"
# Case-insensitive. Also handles new CompanyCam action phrases we
# haven't seen yet (e.g. "Created CompanyCam tag" → "created"),
# which keeps verb lists short even if a future tool adds a novel
# action string. A non-CompanyCam action passes through unchanged.
_CC_TAIL_RE = re.compile(r"\s*(?:on\s+|to\s+)?companycam(?:\s+\w+)?\s*$", re.IGNORECASE)

# Generic single-noun fallback words an integration may set as
# ``target`` when it does not have a human name for the entity
# (archive_project, delete_photo, delete_project, etc.). These are
# universal English nouns, not CompanyCam-specific — any future
# integration using the same fallback approach benefits for free.
# When grouping, prefer any real name over these.
_GENERIC_TARGETS: frozenset[str] = frozenset(
    {"project", "photo", "checklist", "comment", "event", "invoice", "customer", "estimate"}
)


def _pick_group_subject(entries: list[tuple[str, str]]) -> str:
    """Pick the most informative target across a bucket of receipts.

    Real names (e.g. "Smith Residence", "kitchen demo") win over the
    generic fallback words used by archive/delete/notepad tools. When
    every entry is generic, keep the last entry's target so behaviour
    is stable.

    This is what lets `create Smith Residence + update_notepad + archive`
    render with "Smith Residence" as the block subject instead of the
    generic "project" from the archive receipt.
    """
    for _action, target in entries:
        cleaned = _scrub(target)
        if cleaned and cleaned not in _GENERIC_TARGETS:
            return target
    return entries[-1][1]


def _verb_phrase(action: str) -> str:
    """Reduce an action string to the bare verb for a grouped receipt."""
    stripped = _CC_TAIL_RE.sub("", action)
    return stripped.strip().lower()


def _render_group(
    entries: list[tuple[str, str]],
    target: str,
    url: str,
) -> str:
    """Render a set of same-URL receipts as one three-line block.

    ``entries`` is a list of (action, target) pairs, ordered by their
    original appearance. ``target`` is the chosen block subject (see
    ``_pick_group_subject``). Each entry contributes a verb phrase to
    the joined second line, with a parenthesised target when the
    per-entry target differs from the subject.
    """
    clean_subject = _scrub(target)
    verbs: list[str] = []
    for action, entry_target in entries:
        verb = _verb_phrase(action)
        clean_entry_target = _scrub(entry_target)
        if (
            clean_entry_target
            and clean_entry_target != clean_subject
            and clean_entry_target not in _GENERIC_TARGETS
        ):
            verb = f"{verb} ({clean_entry_target})"
        if verb:
            verbs.append(verb)

    head = f"- {_scrub(target)}".rstrip()
    clean_url = _scrub(_display_url(url))
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
            lines.append(_render_group(entries, _pick_group_subject(entries), url))

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

    Before appending, scrub LLM-fabricated receipt bullets from
    ``reply_text`` (see :func:`_strip_fabricated_receipts`). The LLM is
    instructed not to restate tool confirmations but does so anyway,
    pattern-matching the tool result content into a bullet of its own.
    Without this scrub the user sees two receipts: the LLM's restatement
    and the canonical block we append below.
    """
    block = format_receipts_block(tool_calls)
    if not block:
        return reply_text
    cleaned = _strip_fabricated_receipts(reply_text, tool_calls)
    if not cleaned:
        return block
    return f"{cleaned}{_SUMMARY_SEPARATOR}{block}"


# Action verbs the LLM reaches for when it fabricates a receipt-shaped
# bullet from a tool result. Past-tense, lowercase. Restricted to verbs
# that read as receipt confirmations rather than generic English (no
# "saved", "added", "set", "moved", "named", "organized") to avoid
# stripping legitimate user-facing prose like "Saved $5k by switching
# estimate templates" or "Added 3 hours to the labor estimate".
#
# The verb set is intentionally global rather than auto-derived from the
# receipts in scope: the LLM frequently uses a synonym for the receipt's
# verb (receipt says "Scheduled calendar event", LLM bullet says
# "Created calendar event"), and the strip should catch that.
_FABRICATED_RECEIPT_VERBS: frozenset[str] = frozenset(
    {
        "archived",
        "canceled",
        "cancelled",
        "created",
        "deleted",
        "emailed",
        "filed",
        "modified",
        "posted",
        "removed",
        "replied",
        "scheduled",
        "sent",
        "submitted",
        "tagged",
        "updated",
        "uploaded",
    }
)

# Stopwords pulled from receipt action strings before they become strip
# nouns. Keeps "Sent email via Gmail" from contributing "via" as a noun.
_RECEIPT_ACTION_STOPWORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "and",
        "as",
        "at",
        "by",
        "for",
        "from",
        "in",
        "of",
        "on",
        "or",
        "the",
        "to",
        "via",
        "with",
    }
)

_BULLET_RE = re.compile(r"^\s*-\s+(\w+)(.*)$")
_INDENT_RE = re.compile(r"^\s{2,}\S")
_WORD_SPLIT_RE = re.compile(r"[^\w]+")


def _bullet_words(rest: str) -> set[str]:
    """Lowercased word tokens from the part of a bullet after the verb.

    Word-boundary tokenization keeps "Sent emails out" from matching a
    strip noun "email" (substring) when the LLM's "emails" is a different
    word about a different action. Mirrors the splitter used for receipt
    actions so both sides see the same notion of "a word".
    """
    return {w.lower() for w in _WORD_SPLIT_RE.split(rest) if w}


def _strip_nouns_from_receipts(tool_calls: list[StoredToolInteraction]) -> set[str]:
    """Pull strip-eligible nouns out of this turn's actual receipts.

    Each receipt's ``action`` string is split into words; the first word
    is the verb and is dropped (the verb set is global, see above), and
    the remainder contributes content nouns. Stopwords ("via", "to",
    "for") and short tokens are filtered. Lowercased. Empty when the
    turn produced no real receipts.

    Auto-derivation replaces the hand-maintained noun list this filter
    used to ship. New integrations (a Gmail ``Sent email via Gmail``
    action, a future Slack ``Posted message to channel`` action) get
    coverage for free: their action verbiage IS the strip vocabulary
    for that turn. Conservative by construction because the match is
    only valid against actions that actually fired in this turn, so an
    unrelated bullet using the same noun in a non-receipt turn is left
    alone (the outer strip is already gated on ``has_real_receipt``).
    """
    nouns: set[str] = set()
    for tc in tool_calls:
        if tc.is_error or tc.receipt is None or not tc.receipt.action:
            continue
        words = [w.lower() for w in _WORD_SPLIT_RE.split(tc.receipt.action) if w]
        for word in words[1:]:  # skip the verb
            if len(word) >= 3 and word not in _RECEIPT_ACTION_STOPWORDS:
                nouns.add(word)
    return nouns


def _strip_fabricated_receipts(
    reply_text: str,
    tool_calls: list[StoredToolInteraction],
) -> str:
    """Remove receipt-shaped bullets the LLM fabricated in ``reply_text``.

    Triggers only when at least one tool call this turn produced a real
    receipt, so on tool-less responses (or read-only tool turns) we never
    touch the LLM's output. A bullet qualifies for stripping when it
    starts with ``-`` followed by a known write-side action verb AND
    mentions a noun pulled from a real receipt that fired this turn
    (see :func:`_strip_nouns_from_receipts`). Any indented continuation
    line immediately after a stripped bullet (the LLM's "Thu Apr 30,
    12:00 PM" date line) is also dropped.

    Trailing blank lines created by the strip are collapsed so the
    canonical receipt block joins cleanly afterward.
    """
    if not reply_text:
        return reply_text
    nouns = _strip_nouns_from_receipts(tool_calls)
    if not nouns:
        return reply_text

    lines = reply_text.split("\n")
    kept: list[str] = []
    drop_indented = False
    for line in lines:
        if drop_indented:
            if _INDENT_RE.match(line):
                continue
            drop_indented = False
        match = _BULLET_RE.match(line)
        if match:
            verb = match.group(1).lower()
            rest_words = _bullet_words(match.group(2))
            if verb in _FABRICATED_RECEIPT_VERBS and not rest_words.isdisjoint(nouns):
                drop_indented = True
                continue
        kept.append(line)

    while kept and not kept[-1].strip():
        kept.pop()
    return "\n".join(kept)
