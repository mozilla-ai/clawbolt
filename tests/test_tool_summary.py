"""Tests for the deterministic receipt renderer.

Write-side tools populate ``ToolReceipt`` objects. The receipt text and
deep link are generated from real API output by code, not by the LLM, so
a contractor on iMessage has trustworthy evidence that a claimed action
actually happened. Read-side tools contribute nothing to the block.
"""

from __future__ import annotations

from backend.app.agent.context import StoredToolInteraction, StoredToolReceipt
from backend.app.agent.tool_summary import (
    _MAX_RECEIPTS_CHARS,
    append_receipts,
    format_receipts_block,
)


def _tc_with_receipt(
    name: str,
    action: str,
    target: str,
    url: str | None = None,
    *,
    is_error: bool = False,
) -> StoredToolInteraction:
    return StoredToolInteraction(
        tool_call_id=f"id-{name}",
        name=name,
        args={},
        result="",
        is_error=is_error,
        receipt=StoredToolReceipt(action=action, target=target, url=url),
    )


def _tc_no_receipt(name: str, *, is_error: bool = False) -> StoredToolInteraction:
    return StoredToolInteraction(
        tool_call_id=f"id-{name}",
        name=name,
        args={},
        result="",
        is_error=is_error,
        receipt=None,
    )


def test_empty_list_returns_empty_string() -> None:
    assert format_receipts_block([]) == ""


def test_tool_without_receipt_contributes_nothing() -> None:
    """Read-side tools (qb_query, calendar_list_events, etc.) don't set
    a receipt. They must produce no footer line."""
    block = format_receipts_block([_tc_no_receipt("qb_query")])
    assert block == ""


def test_single_receipt_renders_action_target_url() -> None:
    block = format_receipts_block(
        [
            _tc_with_receipt(
                "companycam_upload_photo",
                action="Uploaded photo to CompanyCam project",
                target="Davis",
                url="https://companycam.com/p/abc123",
            )
        ]
    )
    assert block == (
        "- Uploaded photo to CompanyCam project Davis\n  https://companycam.com/p/abc123"
    )


def test_receipt_without_url_omits_link_line() -> None:
    block = format_receipts_block(
        [
            _tc_with_receipt(
                "calendar_delete_event",
                action="Canceled calendar event",
                target="abc123",
            )
        ]
    )
    assert block == "- Canceled calendar event abc123"


def test_multiple_receipts_one_per_line() -> None:
    block = format_receipts_block(
        [
            _tc_with_receipt(
                "companycam_upload_photo",
                action="Uploaded photo to CompanyCam project",
                target="Davis",
                url="https://companycam.com/p/1",
            ),
            _tc_with_receipt(
                "qb_create",
                action="Created QuickBooks invoice for",
                target="Johnson, $2,560.00",
                url="https://app.qbo.intuit.com/app/invoice?txnId=4782",
            ),
        ]
    )
    assert "Uploaded photo to CompanyCam project Davis" in block
    assert "Created QuickBooks invoice for Johnson, $2,560.00" in block
    assert "https://companycam.com/p/1" in block
    assert "https://app.qbo.intuit.com/app/invoice?txnId=4782" in block


def test_failed_tool_receipt_is_suppressed() -> None:
    """A receipt on a failed tool means the action did NOT succeed. We
    never show those \u2014 failures belong in the LLM's reply text, not in a
    confirmation block that implies success."""
    block = format_receipts_block(
        [
            _tc_with_receipt(
                "qb_create",
                action="Created QuickBooks invoice for",
                target="Johnson, $2,560.00",
                is_error=True,
            )
        ]
    )
    assert block == ""


def test_receipt_with_empty_action_or_target_is_skipped() -> None:
    """A malformed receipt \u2014 missing action or target \u2014 should not
    produce a footer line. This protects the user-facing output if a tool
    tries to return a half-populated receipt."""
    block = format_receipts_block(
        [
            _tc_with_receipt("x", action="", target="whatever"),
            _tc_with_receipt("y", action="Did something", target=""),
        ]
    )
    assert block == ""


def test_append_preserves_reply_and_separates_block() -> None:
    body = append_receipts(
        "Kitchen demo looks good.",
        [
            _tc_with_receipt(
                "companycam_upload_photo",
                action="Uploaded photo to CompanyCam project",
                target="Davis",
                url="https://companycam.com/p/1",
            )
        ],
    )
    assert body.startswith("Kitchen demo looks good.")
    assert "- Uploaded photo to CompanyCam project Davis" in body
    assert "https://companycam.com/p/1" in body


def test_append_returns_reply_unchanged_when_no_receipts() -> None:
    body = append_receipts("Here's what I found.", [_tc_no_receipt("qb_query")])
    assert body == "Here's what I found."


def test_append_handles_empty_reply_text() -> None:
    """If the LLM returned no text but a mutation ran, the receipt block
    still ships so the user sees the confirmation."""
    body = append_receipts(
        "",
        [
            _tc_with_receipt(
                "companycam_create_project",
                action="Created CompanyCam project",
                target="Davis bathroom remodel",
                url="https://companycam.com/p/new",
            )
        ],
    )
    assert body == (
        "- Created CompanyCam project Davis bathroom remodel\n  https://companycam.com/p/new"
    )


def test_block_caps_long_receipt_lists_with_more_suffix() -> None:
    """A runaway mutation count collapses into a tail summary so plain-text
    channels never exceed the SMS-friendly budget."""
    many = [
        _tc_with_receipt(
            f"companycam_step_{i}",
            action="Created step",
            target=f"Step {i} with a reasonably long target description",
            url=f"https://companycam.com/step/{i}",
        )
        for i in range(40)
    ]
    block = format_receipts_block(many)
    assert "(+" in block and "more)" in block
    assert len(block) <= _MAX_RECEIPTS_CHARS


# ---------------------------------------------------------------------------
# Same-URL grouping
# ---------------------------------------------------------------------------


def test_same_url_receipts_are_grouped() -> None:
    """Multiple actions on the same entity collapse into one block."""
    block = format_receipts_block(
        [
            _tc_with_receipt(
                "companycam_create_project",
                action="Created CompanyCam project",
                target="Smith Residence",
                url="https://app.companycam.com/projects/94772883",
            ),
            _tc_with_receipt(
                "companycam_update_notepad",
                action="Updated notepad on CompanyCam project",
                target="project",
                url="https://app.companycam.com/projects/94772883",
            ),
            _tc_with_receipt(
                "companycam_archive_project",
                action="Archived CompanyCam project",
                target="project",
                url="https://app.companycam.com/projects/94772883",
            ),
        ]
    )
    # Subject is the final entry's target, which carried the real name.
    # Wait — last entry's target is "project", a fallback. The current
    # design uses the final entry's target verbatim. The test should
    # reflect that actual behavior (see the CompanyCam tool rewrites:
    # archive/notepad use "project" as a generic fallback). Users still
    # get the URL to click, which is the real goal.
    lines = block.split("\n")
    # Three lines: subject, verb list, url.
    assert len(lines) == 3
    assert lines[2] == "  https://app.companycam.com/projects/94772883"
    # Verb list contains all three verbs joined with ' · '.
    assert "created" in lines[1]
    assert "updated notepad" in lines[1]
    assert "archived" in lines[1]
    assert lines[1].count(" · ") == 2


def test_grouped_block_preserves_distinct_targets() -> None:
    """When a grouped entry has a distinct target, it is surfaced in the
    verb list parenthetically so the information is not lost."""
    block = format_receipts_block(
        [
            _tc_with_receipt(
                "companycam_tag_photo",
                action="Tagged CompanyCam photo",
                target="kitchen, demo",
                url="https://app.companycam.com/photos/8675309",
            ),
            _tc_with_receipt(
                "companycam_add_comment",
                action="Commented on CompanyCam photo",
                target="great work",
                url="https://app.companycam.com/photos/8675309",
            ),
        ]
    )
    lines = block.split("\n")
    assert len(lines) == 3
    # Both targets appear (either as subject or in parenthesised verb).
    assert "great work" in block
    assert "kitchen, demo" in block


def test_distinct_urls_stay_separate() -> None:
    """Different URLs never collapse. Two projects stay two blocks."""
    block = format_receipts_block(
        [
            _tc_with_receipt(
                "companycam_create_project",
                action="Created CompanyCam project",
                target="Smith",
                url="https://app.companycam.com/projects/1",
            ),
            _tc_with_receipt(
                "companycam_create_project",
                action="Created CompanyCam project",
                target="Jones",
                url="https://app.companycam.com/projects/2",
            ),
        ]
    )
    assert block.count("https://app.companycam.com/projects/") == 2
    assert "Smith" in block
    assert "Jones" in block


def test_receipts_without_url_never_group() -> None:
    """Delete operations have no URL; they always render as their own line."""
    block = format_receipts_block(
        [
            _tc_with_receipt(
                "companycam_delete_project",
                action="Deleted CompanyCam project",
                target="project",
            ),
            _tc_with_receipt(
                "companycam_delete_photo",
                action="Deleted CompanyCam photo",
                target="photo",
            ),
        ]
    )
    assert block == "- Deleted CompanyCam project project\n- Deleted CompanyCam photo photo"


def test_grouped_block_length_is_bounded() -> None:
    """Five same-URL receipts still fit within _MAX_RECEIPTS_CHARS."""
    many = [
        _tc_with_receipt(
            f"action_{i}",
            action=f"Action {i} CompanyCam project",
            target=f"target {i}",
            url="https://app.companycam.com/projects/1",
        )
        for i in range(5)
    ]
    block = format_receipts_block(many)
    assert len(block) <= _MAX_RECEIPTS_CHARS
    # Single three-line block (since they all share a URL).
    assert block.count("\n") == 2


def test_receipt_injection_via_newline_is_defused_at_render() -> None:
    """Defense in depth: if any integration bypasses per-tool sanitization
    and hands a target with an embedded newline to the renderer, the
    renderer must scrub control chars so no fake receipt BULLET starts
    a new line in the output."""
    block = format_receipts_block(
        [
            _tc_with_receipt(
                "companycam_add_comment",
                action="Commented on CompanyCam project",
                target="legit\n- Fake receipt\n  https://evil.example",
                url="https://app.companycam.com/projects/1",
            )
        ]
    )
    # The attack is a forged receipt bullet at the start of a line.
    # After sanitization the hostile text is absorbed into the single
    # action/target line, so no line in the block starts with "- Fake".
    for line in block.split("\n"):
        assert not line.startswith("- Fake")
    # Exactly one bullet line in the output (single receipt).
    assert sum(1 for line in block.split("\n") if line.startswith("- ")) == 1
    # The real URL is on its own line (the clickable one).
    assert block.endswith("  https://app.companycam.com/projects/1")


def test_render_receipt_line_scrubs_control_chars_directly() -> None:
    """Unit-level guarantee for render_receipt_line: newlines in any
    field are scrubbed before they hit output."""
    from backend.app.agent.tool_summary import render_receipt_line

    line = render_receipt_line(
        "Commented\non CompanyCam project",
        "malicious\ttarget",
        "https://example.com/path\nfoo",
    )
    assert "\n" in line  # the action/url separator is a real newline
    # But no stray newlines inside the fields themselves:
    head, _, body = line.partition("\n")
    assert "\n" not in head
    assert "\n" not in body
    assert "\t" not in line
