"""Tests for the deterministic tool-call summary formatter.

The summary is the failsafe that lets a user on a plain-text channel
(iMessage, SMS, Telegram) see which tools actually ran, independent of
whatever the LLM claims in its reply text.
"""

from __future__ import annotations

from backend.app.agent.context import StoredToolInteraction
from backend.app.agent.tool_summary import (
    _MAX_SUMMARY_CHARS,
    append_tool_call_summary,
    format_tool_call_summary,
)
from backend.app.agent.tools.base import ToolTags
from backend.app.agent.tools.names import ToolName


def _tc(
    name: str, *, is_error: bool = False, tags: set[str] | None = None
) -> StoredToolInteraction:
    return StoredToolInteraction(
        tool_call_id=f"id-{name}",
        name=name,
        args={},
        result="",
        is_error=is_error,
        tags=tags or set(),
    )


def test_empty_list_returns_empty_string() -> None:
    assert format_tool_call_summary([]) == ""


def test_single_tool_renders_label() -> None:
    summary = format_tool_call_summary([_tc("upload_photo")])
    assert summary == "Tools used: upload photo"


def test_multiple_tools_are_comma_separated() -> None:
    summary = format_tool_call_summary([_tc("upload_photo"), _tc("qb_create_invoice")])
    assert summary == "Tools used: upload photo, QuickBooks: create invoice"


def test_failed_tool_is_annotated() -> None:
    summary = format_tool_call_summary(
        [_tc("upload_photo"), _tc("qb_create_invoice", is_error=True)]
    )
    assert summary == "Tools used: upload photo, QuickBooks: create invoice (failed)"


def test_companycam_prefix_is_prettified() -> None:
    summary = format_tool_call_summary([_tc("companycam_upload_photo")])
    assert summary == "Tools used: CompanyCam: upload photo"


def test_quickbooks_prefix_variant_is_prettified() -> None:
    """Both ``qb_`` and ``quickbooks_`` prefixes should render as
    'QuickBooks:' so internal naming churn does not leak to users."""
    short = format_tool_call_summary([_tc("qb_create_invoice")])
    full = format_tool_call_summary([_tc("quickbooks_create_invoice")])
    assert short == "Tools used: QuickBooks: create invoice"
    assert full == "Tools used: QuickBooks: create invoice"


def test_unknown_prefix_falls_back_to_underscore_replacement() -> None:
    """Tools without a known integration prefix still get underscores
    replaced with spaces so they read as natural language."""
    summary = format_tool_call_summary([_tc("memory_recall_facts")])
    assert summary == "Tools used: memory recall facts"


def test_list_capabilities_is_hidden() -> None:
    """list_capabilities is infrastructure, not a user-facing action.
    Surfacing it in the summary would add noise."""
    summary = format_tool_call_summary([_tc(ToolName.LIST_CAPABILITIES), _tc("upload_photo")])
    assert summary == "Tools used: upload photo"


def test_only_hidden_tools_returns_empty_string() -> None:
    summary = format_tool_call_summary([_tc(ToolName.LIST_CAPABILITIES)])
    assert summary == ""


def test_sends_reply_tagged_tools_are_hidden() -> None:
    """A tool tagged SENDS_REPLY IS the reply; listing it would be
    redundant and confusing ('Tools used: send_message' after a send_message
    reply)."""
    summary = format_tool_call_summary([_tc("send_message", tags={ToolTags.SENDS_REPLY})])
    assert summary == ""


def test_append_preserves_reply_and_separates_summary() -> None:
    body = append_tool_call_summary("Done.", [_tc("upload_photo")])
    assert body.startswith("Done.")
    assert body.endswith("Tools used: upload photo")
    assert "\n\n---\n" in body


def test_append_returns_reply_unchanged_when_nothing_to_summarize() -> None:
    body = append_tool_call_summary("Done.", [])
    assert body == "Done."


def test_append_returns_reply_unchanged_when_only_hidden_tools() -> None:
    body = append_tool_call_summary(
        "Here's what I know.",
        [_tc(ToolName.LIST_CAPABILITIES)],
    )
    assert body == "Here's what I know."


def test_append_handles_empty_reply_text() -> None:
    """If reply_text is empty but tools ran, the summary still ships
    as the message body (rather than a stray separator with nothing above)."""
    body = append_tool_call_summary("", [_tc("upload_photo")])
    assert body == "Tools used: upload photo"


def test_summary_caps_long_tool_lists_with_more_suffix() -> None:
    """A runaway tool count should not blow past SMS multipart thresholds.
    Past the cap, remaining tools collapse into ``(+K more)``."""
    many = [_tc(f"companycam_step_{i}") for i in range(30)]
    summary = format_tool_call_summary(many)
    assert summary.startswith("Tools used: ")
    assert "(+" in summary and "more)" in summary
    assert len(summary) <= _MAX_SUMMARY_CHARS + len("Tools used: ")


def test_summary_under_cap_lists_all_tools() -> None:
    """Short lists are listed in full, no truncation."""
    few = [_tc("upload_photo"), _tc("qb_create_invoice")]
    summary = format_tool_call_summary(few)
    assert "(+" not in summary
    assert "more)" not in summary
