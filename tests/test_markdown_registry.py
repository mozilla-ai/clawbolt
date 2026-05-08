"""Tests for the bounded-growth markdown registry.

Pure unit tests for the registry helpers. The integration paths
(workspace tools rejecting over-budget writes, compaction logging on
budget failure, HISTORY.md windowing on append) are exercised in
``test_workspace_tools.py``, ``test_compaction.py``, and
``test_memory_db_async.py`` respectively.

The consistency tests below are the load-bearing piece: they make it
hard to add a new agent-mutable markdown surface without also adding
a policy entry, which is the mechanism that prevents future features
from silently re-introducing unbounded growth.
"""

from __future__ import annotations

import pytest

from backend.app.agent.markdown_registry import (
    COLUMN_TO_SURFACE,
    DEFAULT_BUDGET,
    POLICIES,
    BudgetExceededError,
    StorageKind,
    WriteMode,
    append_with_window,
    assert_column_within_budget,
    assert_within_budget,
    get_policy,
    policy_for_column,
    truncate_for_injection,
)

# ---------------------------------------------------------------------------
# Registry consistency
# ---------------------------------------------------------------------------


def test_every_known_surface_has_a_policy() -> None:
    """The six surfaces named in the audit must each have a policy entry."""
    expected = {
        "USER.md",
        "SOUL.md",
        "HEARTBEAT.md",
        "MEMORY.md",
        "HISTORY.md",
        "BOOTSTRAP.md",
    }
    assert expected.issubset(POLICIES.keys()), f"missing policies: {expected - POLICIES.keys()}"


def test_every_policy_has_a_positive_budget() -> None:
    """Every declared surface must have a non-zero byte budget.

    A surface entered into the registry without a budget would silently
    skip enforcement, defeating the purpose of the registry. This test
    is the guard that prevents that mistake.
    """
    for name, policy in POLICIES.items():
        assert policy.byte_budget > 0, f"{name} has non-positive budget"


def test_default_budget_matches_25kib() -> None:
    """The uniform 25 KiB cap is intentional. Tests pin it so future
    edits to the constant are made consciously, not by accident."""
    assert DEFAULT_BUDGET == 25 * 1024


def test_column_to_surface_round_trip() -> None:
    """Every COLUMN_TO_SURFACE entry must point at a real policy."""
    for column, surface in COLUMN_TO_SURFACE.items():
        policy = POLICIES[surface]
        assert policy.storage_ref == column, (
            f"COLUMN_TO_SURFACE[{column!r}]={surface!r} disagrees with "
            f"POLICIES[{surface!r}].storage_ref={policy.storage_ref!r}"
        )


def test_history_md_is_the_only_append_surface() -> None:
    """If a future surface is added with append mode, it must explicitly
    declare a windowing strategy. This pins the current set so that
    decision is forced into review."""
    append_only = {n for n, p in POLICIES.items() if p.write_mode is WriteMode.APPEND}
    assert append_only == {"HISTORY.md"}


def test_history_md_is_not_injected_into_prompts() -> None:
    """If HISTORY.md ever starts being injected into a prompt, the
    windowing budget needs to be re-evaluated against prompt-cost
    impact, not just storage size. Force that decision through review."""
    assert POLICIES["HISTORY.md"].injected_into_prompt is False


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------


def test_get_policy_returns_none_for_unknown() -> None:
    assert get_policy("DOES_NOT_EXIST.md") is None


def test_policy_for_column_returns_none_for_unknown() -> None:
    assert policy_for_column("nonexistent_column") is None


def test_policy_for_column_resolves_each_known_column() -> None:
    for column in COLUMN_TO_SURFACE:
        assert policy_for_column(column) is not None


# ---------------------------------------------------------------------------
# assert_within_budget
# ---------------------------------------------------------------------------


def test_assert_within_budget_passes_for_under_budget_content() -> None:
    assert_within_budget("USER.md", "small content")


def test_assert_within_budget_unknown_surface_is_noop() -> None:
    """Unknown surfaces silently pass. The registry is opt-in: code that
    does not route through the registry is not policed by it."""
    assert_within_budget("ARBITRARY.md", "x" * (DEFAULT_BUDGET * 10))


def test_assert_within_budget_raises_on_overflow() -> None:
    too_big = "x" * (DEFAULT_BUDGET + 1)
    with pytest.raises(BudgetExceededError) as exc_info:
        assert_within_budget("USER.md", too_big)
    assert exc_info.value.surface_name == "USER.md"
    assert exc_info.value.size_bytes == DEFAULT_BUDGET + 1
    assert exc_info.value.budget_bytes == DEFAULT_BUDGET


def test_assert_within_budget_counts_utf8_bytes_not_chars() -> None:
    """Multibyte characters must count by their UTF-8 byte cost, not
    code-point count, because that is what hits the LLM token budget.

    "💼" encodes as 4 UTF-8 bytes; ``DEFAULT_BUDGET // 4 + 1`` of them
    take ``DEFAULT_BUDGET + 4`` bytes, exceeding the cap, even though
    the character count is well under the cap.
    """
    payload = "💼" * (DEFAULT_BUDGET // 4 + 1)
    assert len(payload) < DEFAULT_BUDGET  # under by character count
    with pytest.raises(BudgetExceededError):
        assert_within_budget("USER.md", payload)


def test_assert_column_within_budget_routes_through_column_map() -> None:
    too_big = "x" * (DEFAULT_BUDGET + 1)
    with pytest.raises(BudgetExceededError) as exc_info:
        assert_column_within_budget("user_text", too_big)
    assert exc_info.value.surface_name == "USER.md"


def test_assert_column_within_budget_unknown_column_is_noop() -> None:
    assert_column_within_budget("not_a_column", "x" * (DEFAULT_BUDGET * 10))


# ---------------------------------------------------------------------------
# truncate_for_injection
# ---------------------------------------------------------------------------


def test_truncate_for_injection_returns_original_when_under_budget() -> None:
    content = "the original content"
    assert truncate_for_injection("USER.md", content) == content


def test_truncate_for_injection_returns_original_for_unknown_surface() -> None:
    huge = "x" * (DEFAULT_BUDGET * 10)
    assert truncate_for_injection("UNKNOWN.md", huge) == huge


def test_truncate_for_injection_keeps_tail_and_prepends_marker() -> None:
    """An over-budget value must be tail-truncated to the budget and
    prefixed with a marker the agent can read."""
    body = "x" * 1000 + "TAIL_MARKER" + "y" * 100
    long = body * 50
    truncated = truncate_for_injection("USER.md", long)
    assert truncated.startswith("[truncated:")
    assert "TAIL_MARKER" in truncated  # tail content preserved
    assert len(truncated.encode("utf-8")) <= DEFAULT_BUDGET


def test_truncate_for_injection_handles_partial_multibyte_at_cut() -> None:
    """Cutting on a UTF-8 byte boundary that lands inside a multibyte
    sequence must not raise; ``errors="ignore"`` drops the partial
    leading sequence cleanly."""
    payload = "💼" * (DEFAULT_BUDGET // 2)  # 2 * (DEFAULT_BUDGET // 2) bytes-ish
    out = truncate_for_injection("USER.md", payload)
    out.encode("utf-8")  # round-trips without error


# ---------------------------------------------------------------------------
# append_with_window
# ---------------------------------------------------------------------------


def _entry(timestamp: str, body: str) -> str:
    return f"[{timestamp}] {body}"


def test_append_with_window_under_budget_keeps_everything() -> None:
    current = _entry("2026-01-01 00:00", "first") + "\n"
    out = append_with_window(current, _entry("2026-01-02 00:00", "second"), DEFAULT_BUDGET)
    assert "first" in out
    assert "second" in out


def test_append_with_window_drops_oldest_entries_when_over_budget() -> None:
    """Once the post-append text exceeds the budget, the oldest
    timestamped entries are dropped whole until it fits again."""
    # Build a current text with many small entries that already nearly fill
    # the budget, then append one more so the total exceeds the budget.
    entries = [_entry(f"2026-01-{i:02d} 00:00", "x" * 200) for i in range(1, 130)]
    current = "\n".join(entries) + "\n"
    new = _entry("2026-02-01 00:00", "newest")
    out = append_with_window(current, new, DEFAULT_BUDGET)
    assert len(out.encode("utf-8")) <= DEFAULT_BUDGET
    assert "newest" in out
    # Older entries should have been dropped first; at least the very
    # first one must be gone.
    assert "[2026-01-01" not in out


def test_append_with_window_falls_back_to_byte_truncation_for_huge_entry() -> None:
    """A single entry exceeding the budget cannot be preserved whole;
    the function falls back to byte-tail truncation so the result is
    still under budget."""
    huge_entry = _entry("2026-03-01 00:00", "x" * (DEFAULT_BUDGET * 2))
    out = append_with_window("", huge_entry, DEFAULT_BUDGET)
    assert len(out.encode("utf-8")) <= DEFAULT_BUDGET


def test_append_with_window_normalizes_missing_separator() -> None:
    """If the pre-existing text lacks a trailing newline, the function
    must add one before appending so two entries do not jam together."""
    current = _entry("2026-01-01 00:00", "no trailing newline")  # no \n
    out = append_with_window(current, _entry("2026-01-02 00:00", "second"), DEFAULT_BUDGET)
    # The two entries must not be on the same line.
    lines_with_brackets = [line for line in out.splitlines() if line.startswith("[")]
    assert len(lines_with_brackets) == 2


def test_append_with_window_repeated_appends_stay_bounded() -> None:
    """Repeated append calls must keep the cumulative text under
    budget. This is the single test that most closely matches the
    issue's acceptance criterion: 'tests cover repeated-update growth
    behavior for the important files'."""
    current = ""
    big_chunk = "x" * 1000
    for i in range(500):
        current = append_with_window(
            current, _entry(f"2026-04-{(i % 28) + 1:02d} 00:00", big_chunk), DEFAULT_BUDGET
        )
        assert len(current.encode("utf-8")) <= DEFAULT_BUDGET, (
            f"history exceeded budget at iteration {i}: {len(current.encode('utf-8'))} bytes"
        )


# ---------------------------------------------------------------------------
# StorageKind / WriteMode integration
# ---------------------------------------------------------------------------


def test_storage_kinds_cover_each_kind_used() -> None:
    """Each StorageKind value must be used by at least one policy.
    Keeps the enum honest: an orphaned variant is a hint that a
    surface was deleted and the policy entry was not."""
    used = {p.storage for p in POLICIES.values()}
    assert used == set(StorageKind)
