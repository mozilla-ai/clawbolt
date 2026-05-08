"""Bounded-growth registry for agent-managed markdown surfaces.

Single source of truth for every markdown file the agent can read or
write. Each surface declares:

- where it is stored (which DB column or disk path);
- how it is written (full rewrite, append, transient);
- whether the contents are injected into an LLM prompt;
- a hard byte budget enforced at write time;
- a one-line description of what the file is for.

The point of this registry is not just data: it is the integration
seam used by the write paths (``workspace_tools``, ``memory_db``,
``stores``), the read paths (``system_prompt`` builders), and the
regression tests. Adding a new agent-mutable markdown surface without
adding it here will fail the ``test_markdown_registry`` consistency
tests, which is the mechanism that prevents future features from
silently re-introducing unbounded growth.

See ``docs/markdown_growth_policies.md`` for the per-surface rationale
and the prior-art references the policies are based on (Claude Code's
25 KB MEMORY.md cap, Letta / MemGPT's per-block character limits).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import StrEnum

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


class StorageKind(StrEnum):
    """Where a markdown surface is persisted."""

    USER_COLUMN = "user_column"
    """Plaintext ``Text`` column on the ``users`` row."""

    MEMORY_COLUMN = "memory_column"
    """``EncryptedString`` column on the ``memory_documents`` row."""

    DISK_FILE = "disk_file"
    """Plaintext file under ``data/users/{user_id}/``."""


class WriteMode(StrEnum):
    """How the agent (or compaction) writes the surface."""

    REWRITE = "rewrite"
    """Full overwrite. Each write replaces the previous content."""

    APPEND = "append"
    """Accumulating log. Each write appends a new entry; prior entries persist."""

    TRANSIENT = "transient"
    """Written once on provision, removed when no longer relevant."""


@dataclass(frozen=True, slots=True)
class MarkdownPolicy:
    """Bounded-growth policy for one agent-managed markdown surface."""

    name: str
    """Canonical filename, e.g. ``USER.md`` (case-sensitive, used as registry key)."""

    storage: StorageKind
    storage_ref: str
    """Column name (for ``*_COLUMN``) or relative path (for ``DISK_FILE``)."""

    write_mode: WriteMode
    byte_budget: int
    """Hard cap in UTF-8 bytes. Same value across surfaces (see DEFAULT_BUDGET)."""

    injected_into_prompt: bool
    """True when the contents land in an LLM system prompt verbatim."""

    description: str


class BudgetExceededError(ValueError):
    """Raised when a write would push a markdown surface past its byte budget.

    Carries the surface name and the actual / allowed sizes so callers
    (workspace tools, compaction) can produce a useful message back to
    the agent or the operator log without re-counting bytes.
    """

    def __init__(self, surface_name: str, size_bytes: int, budget_bytes: int) -> None:
        over = size_bytes - budget_bytes
        super().__init__(
            f"{surface_name} write of {size_bytes} bytes exceeds the "
            f"{budget_bytes}-byte budget by {over} bytes. "
            f"Trim the content to fit within {budget_bytes} bytes."
        )
        self.surface_name = surface_name
        self.size_bytes = size_bytes
        self.budget_bytes = budget_bytes


# ---------------------------------------------------------------------------
# Policies
# ---------------------------------------------------------------------------


# 25 KiB across the board. Matches Claude Code's MEMORY.md cap and the
# existing ``compaction_event_snapshot_max_bytes_per_file`` audit cap,
# so any in-budget surface fits in audit rows without truncation. The
# uniform value is deliberate: it makes the policy memorable and avoids
# bikeshedding per file. Tune individual surfaces only when there is
# evidence that a tighter or looser bound is needed.
DEFAULT_BUDGET: int = 25 * 1024


POLICIES: dict[str, MarkdownPolicy] = {
    "USER.md": MarkdownPolicy(
        name="USER.md",
        storage=StorageKind.USER_COLUMN,
        storage_ref="user_text",
        write_mode=WriteMode.REWRITE,
        byte_budget=DEFAULT_BUDGET,
        injected_into_prompt=True,
        description="User profile facts, injected into every system prompt.",
    ),
    "SOUL.md": MarkdownPolicy(
        name="SOUL.md",
        storage=StorageKind.USER_COLUMN,
        storage_ref="soul_text",
        write_mode=WriteMode.REWRITE,
        byte_budget=DEFAULT_BUDGET,
        injected_into_prompt=True,
        description="Agent identity / personality, injected into every system prompt.",
    ),
    "HEARTBEAT.md": MarkdownPolicy(
        name="HEARTBEAT.md",
        storage=StorageKind.USER_COLUMN,
        storage_ref="heartbeat_text",
        write_mode=WriteMode.REWRITE,
        byte_budget=DEFAULT_BUDGET,
        injected_into_prompt=True,
        description="Active task list, injected into the heartbeat system prompt.",
    ),
    "MEMORY.md": MarkdownPolicy(
        name="MEMORY.md",
        storage=StorageKind.MEMORY_COLUMN,
        storage_ref="memory_text",
        write_mode=WriteMode.REWRITE,
        byte_budget=DEFAULT_BUDGET,
        injected_into_prompt=True,
        description="Working memory, injected into every system prompt.",
    ),
    "HISTORY.md": MarkdownPolicy(
        name="HISTORY.md",
        storage=StorageKind.MEMORY_COLUMN,
        storage_ref="history_text",
        write_mode=WriteMode.APPEND,
        byte_budget=DEFAULT_BUDGET,
        injected_into_prompt=False,
        description="Append-only compaction archive, windowed to byte_budget on append.",
    ),
    "BOOTSTRAP.md": MarkdownPolicy(
        name="BOOTSTRAP.md",
        storage=StorageKind.DISK_FILE,
        storage_ref="BOOTSTRAP.md",
        write_mode=WriteMode.TRANSIENT,
        byte_budget=DEFAULT_BUDGET,
        injected_into_prompt=False,
        description="Onboarding gate file, deleted by OnboardingSubscriber on completion.",
    ),
}


# Map storage column name -> canonical surface name. Used by store
# helpers that know the column they are writing but not the registry
# key. Kept explicit (rather than derived by inverting POLICIES) so a
# disk-backed surface with the same string ref as a column does not
# silently collide.
COLUMN_TO_SURFACE: dict[str, str] = {
    "user_text": "USER.md",
    "soul_text": "SOUL.md",
    "heartbeat_text": "HEARTBEAT.md",
    "memory_text": "MEMORY.md",
    "history_text": "HISTORY.md",
}


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------


def get_policy(name: str) -> MarkdownPolicy | None:
    """Return the policy for *name*, or ``None`` for unknown surfaces."""
    return POLICIES.get(name)


def policy_for_column(column: str) -> MarkdownPolicy | None:
    """Return the policy for the given DB column, or ``None`` if unmapped."""
    surface = COLUMN_TO_SURFACE.get(column)
    if surface is None:
        return None
    return POLICIES.get(surface)


# ---------------------------------------------------------------------------
# Write-time enforcement
# ---------------------------------------------------------------------------


def assert_within_budget(name: str, stored_value: str) -> None:
    """Raise :class:`BudgetExceededError` when ``stored_value`` is over budget.

    ``stored_value`` should be the exact text that will land in storage
    (after any wrapper / trailing newline the writer applies), so the
    enforced size matches what the prompt builders will eventually
    inject. UTF-8 byte count is the canonical size: characters undercount
    non-ASCII content, which still costs LLM tokens.

    No-op for unknown surfaces and for the (currently empty) case of a
    declared policy with no budget. Callers writing to a new file should
    add it to ``POLICIES`` rather than special-casing here.
    """
    policy = POLICIES.get(name)
    if policy is None:
        return
    size = len(stored_value.encode("utf-8"))
    if size > policy.byte_budget:
        raise BudgetExceededError(name, size, policy.byte_budget)


def assert_column_within_budget(column: str, stored_value: str) -> None:
    """Column-keyed convenience wrapper around :func:`assert_within_budget`."""
    surface = COLUMN_TO_SURFACE.get(column)
    if surface is None:
        return
    assert_within_budget(surface, stored_value)


# ---------------------------------------------------------------------------
# Read-time truncation (defence in depth for legacy over-budget rows)
# ---------------------------------------------------------------------------


def truncate_for_injection(name: str, content: str) -> str:
    """Tail-truncate ``content`` to the surface's byte budget for prompt injection.

    Write-time caps prevent new over-budget writes, but rows that
    pre-date the cap (or were written via a path that bypassed it)
    would otherwise still bloat the prompt on the next turn. This
    helper is the read-side defence: every prompt-injection builder
    runs the value through here before handing it to the LLM.

    The tail (most recent content) is kept because for both
    rewrite-mode files (USER.md, SOUL.md, MEMORY.md, HEARTBEAT.md, where
    "most recent" usually means "most relevant") and append-mode files
    the head is the part most likely to be stale. A one-line marker is
    prepended so the agent knows the context was clipped and can
    rewrite the file smaller.

    Cuts on UTF-8 byte boundaries with ``errors="ignore"`` so a partial
    leading multibyte sequence is dropped cleanly rather than producing
    a decode error.
    """
    policy = POLICIES.get(name)
    if policy is None:
        return content
    encoded = content.encode("utf-8")
    if len(encoded) <= policy.byte_budget:
        return content
    marker = (
        f"[truncated: {name} exceeds the {policy.byte_budget}-byte budget; "
        "showing tail. Rewrite this file smaller to restore full visibility.]\n"
    )
    marker_bytes = marker.encode("utf-8")
    keep = max(0, policy.byte_budget - len(marker_bytes))
    tail = encoded[-keep:].decode("utf-8", errors="ignore")
    logger.warning(
        "markdown_registry: truncated %s for prompt injection (size=%d, budget=%d)",
        name,
        len(encoded),
        policy.byte_budget,
    )
    return marker + tail


# ---------------------------------------------------------------------------
# Append-mode windowing (HISTORY.md)
# ---------------------------------------------------------------------------


# Compaction emits each history entry prefixed with ``[YYYY-MM-DD HH:MM]``
# (see ``backend/app/agent/compaction.py`` and ``prompts/compaction.md``).
# We split on lines that begin with ``[`` to find entry boundaries, so
# whole entries can be dropped from the front instead of cutting a
# sentence in half. Also tolerates the legacy case where a manually
# edited history has content before the first timestamp.
_ENTRY_HEAD_RE = re.compile(r"^\[", re.MULTILINE)


def _split_history_entries(text: str) -> list[str]:
    """Split *text* into entries. Each entry retains its trailing newlines.

    An entry begins at a line starting with ``[`` (a timestamp marker).
    Anything before the first such line is returned as a leading
    "preamble" entry so it is not silently lost; in practice, that
    region is empty for compaction-written histories.
    """
    if not text:
        return []
    starts = [m.start() for m in _ENTRY_HEAD_RE.finditer(text)]
    if not starts:
        return [text]
    entries: list[str] = []
    if starts[0] > 0:
        entries.append(text[: starts[0]])
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else len(text)
        entries.append(text[start:end])
    return entries


def append_with_window(current: str, entry: str, budget: int) -> str:
    """Append ``entry`` to ``current`` and drop oldest entries until under budget.

    Used by the HISTORY.md append path so the on-disk archive stays
    bounded without losing the integrity guarantees of the existing
    locked append (issue #1243 / PR #1273): we still produce the full
    plaintext that the locked UPDATE writes, but we prune older entries
    on the way through.

    Entries are dropped whole (FIFO) rather than mid-line so the log
    stays readable and grep-friendly. If a single tail entry still
    exceeds *budget* on its own (e.g. an unusually long compaction
    summary), the function falls back to UTF-8 byte-tail truncation
    to guarantee an under-budget result rather than letting the cap
    silently fail.

    Returns the full new plaintext, including a trailing newline so
    the next append can rely on the standard separator invariant.
    """
    if current and not current.endswith("\n"):
        current = current + "\n"
    if not entry.endswith("\n"):
        entry = entry + "\n"
    text = current + entry

    if len(text.encode("utf-8")) <= budget:
        return text

    entries = _split_history_entries(text)
    while len(entries) > 1 and len("".join(entries).encode("utf-8")) > budget:
        entries.pop(0)
    rebuilt = "".join(entries)
    if len(rebuilt.encode("utf-8")) <= budget:
        return rebuilt

    encoded = rebuilt.encode("utf-8")
    tail = encoded[-budget:].decode("utf-8", errors="ignore")
    if tail and not tail.endswith("\n"):
        tail = tail + "\n"
    return tail


__all__ = [
    "COLUMN_TO_SURFACE",
    "DEFAULT_BUDGET",
    "POLICIES",
    "BudgetExceededError",
    "MarkdownPolicy",
    "StorageKind",
    "WriteMode",
    "append_with_window",
    "assert_column_within_budget",
    "assert_within_budget",
    "get_policy",
    "policy_for_column",
    "truncate_for_injection",
]
