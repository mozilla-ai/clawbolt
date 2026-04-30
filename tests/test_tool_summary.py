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
    assert block == ("- Uploaded photo to CompanyCam project Davis\n  companycam.com/p/abc123")


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
    assert "companycam.com/p/1" in block
    assert "app.qbo.intuit.com/app/invoice?txnId=4782" in block
    assert "https://" not in block


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
    assert "companycam.com/p/1" in body
    assert "https://" not in body


def test_append_returns_reply_unchanged_when_no_receipts() -> None:
    body = append_receipts("Here's what I found.", [_tc_no_receipt("qb_query")])
    assert body == "Here's what I found."


def test_append_strips_llm_fabricated_calendar_bullet() -> None:
    """When the LLM restates a calendar receipt as its own bullet, scrub it.

    Regression for the prod calendar bug observed 2026-04-29: the LLM was
    emitting a "- Created Google Calendar event: Lunch with Tam\\n  Thu Apr
    30, 12:00 PM..." bullet inside reply_text and ``append_receipts``
    appended the canonical "- Scheduled calendar event Lunch with Tam on
    2026-04-30 12:00" below it, so the user saw two receipts per event.
    """
    body = append_receipts(
        (
            "Done! Lunch with Tam is on your calendar for tomorrow at noon.\n\n"
            "- Created Google Calendar event: Lunch with Tam\n"
            "  Thu Apr 30, 12:00 PM – 1:00 PM"  # noqa: RUF001
        ),
        [
            _tc_with_receipt(
                "calendar_create_event",
                action="Scheduled calendar event",
                target="Lunch with Tam on 2026-04-30 12:00",
            )
        ],
    )
    # Fabricated bullet and its indented date line are gone.
    assert "Created Google Calendar event" not in body
    assert "Thu Apr 30" not in body
    # Real receipt is appended.
    assert "- Scheduled calendar event Lunch with Tam on 2026-04-30 12:00" in body
    # Reply preface is preserved.
    assert body.startswith("Done! Lunch with Tam is on your calendar")


def test_append_strips_multiple_fabricated_bullets() -> None:
    """Two events created in one turn produce two fabricated bullets;
    both should be stripped so only the canonical receipts remain.
    """
    body = append_receipts(
        (
            "Both reminders are set.\n\n"
            "- Scheduled calendar event: Call PNC Bank about a vehicle loan\n"
            "  Thu Apr 30, 11:00 AM\n\n"
            "- Scheduled calendar event: Call Maryann and Mark about jobs\n"
            "  Thu Apr 30, 9:00 AM"
        ),
        [
            _tc_with_receipt(
                "calendar_create_event",
                action="Scheduled calendar event",
                target="Call PNC Bank about a vehicle loan on 2026-04-30 11:00",
            ),
            _tc_with_receipt(
                "calendar_create_event",
                action="Scheduled calendar event",
                target="Call Maryann and Mark about jobs on 2026-04-30 09:00",
            ),
        ],
    )
    # Only the canonical receipts should appear, once each.
    assert body.count("- Scheduled calendar event") == 2
    assert "Thu Apr 30, 11:00 AM" not in body
    assert "Thu Apr 30, 9:00 AM" not in body
    assert body.startswith("Both reminders are set.")


def test_append_strips_delete_fabricated_bullet() -> None:
    """The LLM also fabricates "Deleted Google Calendar event:" lines for
    cancellations. Same scrub should catch them."""
    body = append_receipts(
        ("Done, that's off your calendar.\n\n- Deleted Google Calendar event: Lunch with Tam"),
        [
            _tc_with_receipt(
                "calendar_delete_event",
                action="Canceled calendar event",
                target="abc123",
            )
        ],
    )
    assert "Deleted Google Calendar event" not in body
    assert "- Canceled calendar event abc123" in body


def test_append_keeps_unrelated_bullets_when_receipt_present() -> None:
    """A non-receipt bullet ("- Acme: $5k owed") in the LLM's reply must
    survive the scrub. The filter only fires on lines that look like
    fabricated tool receipts (action verb + receipt-flavored noun)."""
    body = append_receipts(
        ("Here's what I found:\n- Acme: $5k owed\n- Smith: paid in full"),
        [
            _tc_with_receipt(
                "qb_create",
                action="Created QuickBooks invoice for",
                target="Davis",
                url="https://app.qbo.intuit.com/app/invoice?txnId=1",
            )
        ],
    )
    assert "- Acme: $5k owed" in body
    assert "- Smith: paid in full" in body


def test_append_does_not_strip_when_no_real_receipt() -> None:
    """Without a real receipt this turn, leave the LLM's text alone.

    The scrub only fires when a canonical receipt block will be appended
    below. If the agent had no write-side tool calls, an LLM bullet that
    happens to start with a receipt-like verb is just user-relevant
    text and must pass through untouched.
    """
    body = append_receipts(
        "- Created a draft of the proposal in your notes.",
        [_tc_no_receipt("read_file")],
    )
    assert body == "- Created a draft of the proposal in your notes."


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
    assert body == ("- Created CompanyCam project Davis bathroom remodel\n  companycam.com/p/new")


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
    """Multiple actions on the same entity collapse into one block.

    The block subject is the most informative target: a real name wins
    over the generic 'project' / 'photo' / 'checklist' fallbacks used
    by archive/delete/notepad tools. Users see 'Smith Residence', not
    'project', even when the last action was archive.
    """
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
    lines = block.split("\n")
    # Three lines: subject, verb list, url.
    assert len(lines) == 3
    # Subject is the real name from the create receipt.
    assert lines[0] == "- Smith Residence"
    assert lines[2] == "  app.companycam.com/projects/94772883"
    # Verb list contains all three verbs joined with ' · '.
    assert "created" in lines[1]
    assert "updated notepad" in lines[1]
    assert "archived" in lines[1]
    assert lines[1].count(" · ") == 2


def test_grouped_subject_falls_back_to_last_when_all_generic() -> None:
    """If every entry has a generic fallback target, keep the last one
    so behaviour is stable (no picking semantics to debate)."""
    block = format_receipts_block(
        [
            _tc_with_receipt(
                "companycam_update_notepad",
                action="Updated notepad on CompanyCam project",
                target="project",
                url="https://app.companycam.com/projects/1",
            ),
            _tc_with_receipt(
                "companycam_archive_project",
                action="Archived CompanyCam project",
                target="project",
                url="https://app.companycam.com/projects/1",
            ),
        ]
    )
    lines = block.split("\n")
    assert lines[0] == "- project"


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
    assert block.count("app.companycam.com/projects/") == 2
    assert "https://" not in block
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
    assert block.endswith("  app.companycam.com/projects/1")


def test_verb_phrase_strips_unseen_companycam_suffixes() -> None:
    """New CompanyCam tools with novel action phrases still get the
    'companycam [noun]' tail stripped so verb lists stay tight."""
    from backend.app.agent.tool_summary import _verb_phrase

    # Known phrases from the plan.
    assert _verb_phrase("Created CompanyCam project") == "created"
    assert _verb_phrase("Archived CompanyCam project") == "archived"
    assert _verb_phrase("Commented on CompanyCam project") == "commented"
    assert _verb_phrase("Uploaded photo to CompanyCam") == "uploaded photo"
    assert _verb_phrase("Tagged CompanyCam photo") == "tagged"
    # Novel action phrases (not enumerated at authorship time).
    assert _verb_phrase("Created CompanyCam tag") == "created"
    assert _verb_phrase("Created CompanyCam label") == "created"
    # Non-CompanyCam action passes through.
    assert _verb_phrase("Scheduled calendar event") == "scheduled calendar event"


def test_grouping_works_across_integrations() -> None:
    """Same-URL grouping is integration-agnostic. QBO and Calendar get
    the same treatment as CompanyCam for free."""
    # QuickBooks: create invoice + email invoice to client, both share the
    # same QBO deep link.
    qbo_url = "https://app.qbo.intuit.com/app/invoice?txnId=4782"
    block = format_receipts_block(
        [
            _tc_with_receipt(
                "qb_create",
                action="Created QuickBooks invoice for",
                target="Johnson, $2,560.00",
                url=qbo_url,
            ),
            _tc_with_receipt(
                "qb_send",
                action="Emailed QuickBooks invoice to",
                target="johnson@example.com",
                url=qbo_url,
            ),
        ]
    )
    lines = block.split("\n")
    # One grouped 3-line block.
    assert len(lines) == 3
    assert "app.qbo.intuit.com/app/invoice?txnId=4782" in lines[2]
    assert "https://" not in block
    # Subject is the first informative target (the invoice with amount).
    assert lines[0] == "- Johnson, $2,560.00"
    # Email recipient survives as a parenthesised qualifier on the email verb.
    assert "johnson@example.com" in lines[1]


def test_calendar_event_delete_is_standalone() -> None:
    """Calendar delete produces a single-line receipt (no URL) alongside
    a grouped project block. The two must not interfere."""
    block = format_receipts_block(
        [
            _tc_with_receipt(
                "calendar_delete_event",
                action="Canceled calendar event",
                target="Kitchen walkthrough",
            ),
            _tc_with_receipt(
                "companycam_create_project",
                action="Created CompanyCam project",
                target="Smith Residence",
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
    # Calendar line is standalone (no URL). CompanyCam pair is one grouped block.
    assert "- Canceled calendar event Kitchen walkthrough\n" in block + "\n"
    assert "- Smith Residence" in block
    assert "created · archived" in block


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


# ---------------------------------------------------------------------------
# Compact URL rendering (issue #976) -- strip https:// prefix
# ---------------------------------------------------------------------------


def test_display_url_strips_https_prefix() -> None:
    """The helper is the one place that decides the compact form."""
    from backend.app.agent.tool_summary import _display_url

    assert (
        _display_url("https://app.companycam.com/projects/42/photos")
        == "app.companycam.com/projects/42/photos"
    )


def test_display_url_passes_through_bare_url() -> None:
    """A URL without a scheme is left alone so auto-linking channels
    still see a bare domain (already compact)."""
    from backend.app.agent.tool_summary import _display_url

    assert _display_url("app.companycam.com/projects/42") == "app.companycam.com/projects/42"


def test_display_url_preserves_http_prefix() -> None:
    """A plain http:// URL is visually suspicious; keeping it visible
    lets the user notice that the link is not over TLS."""
    from backend.app.agent.tool_summary import _display_url

    assert _display_url("http://example.com/x") == "http://example.com/x"


def test_display_url_is_case_sensitive() -> None:
    """removeprefix is case-sensitive by design. No production integration
    emits an uppercase scheme today, so we document rather than mitigate."""
    from backend.app.agent.tool_summary import _display_url

    assert _display_url("HTTPS://app.companycam.com/x") == "HTTPS://app.companycam.com/x"


def test_render_receipt_strips_https_prefix() -> None:
    """End-to-end: a single receipt renders the URL in compact form."""
    from backend.app.agent.tool_summary import render_receipt_line

    rendered = render_receipt_line(
        "Created CompanyCam project",
        "Astro Home Management",
        "https://app.companycam.com/projects/103320586/photos",
    )
    assert "\n  app.companycam.com/projects/103320586/photos" in rendered
    assert "https://" not in rendered


def test_grouped_receipt_strips_https_prefix() -> None:
    """Grouped-receipt path (shared URL) also runs through _display_url."""
    shared = "https://app.companycam.com/projects/42/photos"
    block = format_receipts_block(
        [
            _tc_with_receipt(
                "companycam_create_project",
                action="Created CompanyCam project",
                target="Demo",
                url=shared,
            ),
            _tc_with_receipt(
                "companycam_upload_photo",
                action="Uploaded photo to CompanyCam",
                target="photo",
                url=shared,
            ),
        ]
    )
    assert "app.companycam.com/projects/42/photos" in block
    assert "https://" not in block
