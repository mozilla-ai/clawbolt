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
