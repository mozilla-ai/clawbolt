"""Unit tests for the CompanyCam receipt helpers.

These helpers produce the `target` and `url` fields of a ToolReceipt for
every CompanyCam write-side tool. They must never leak a raw CompanyCam
id into the contractor's iMessage footer, and must never embed newlines
that could forge a fake receipt line.
"""

from __future__ import annotations

from backend.app.agent.tools.companycam_receipts import (
    _sanitize,
    comment_target,
    photo_target,
    photo_url,
    project_target,
    project_url,
    tags_target,
)
from backend.app.services.companycam_models import Photo, Project

# ---------------------------------------------------------------------------
# URL builders
# ---------------------------------------------------------------------------


def test_project_url_numeric_id() -> None:
    assert project_url("94772883") == "https://app.companycam.com/projects/94772883"


def test_project_url_empty_id_returns_none() -> None:
    assert project_url("") is None


def test_project_url_blocks_query_injection() -> None:
    assert project_url("94772883?foo=bar") is None


def test_project_url_blocks_path_traversal() -> None:
    assert project_url("../admin") is None


def test_project_url_blocks_non_numeric() -> None:
    assert project_url("abc123") is None


def test_photo_url_numeric_id() -> None:
    assert photo_url("8675309") == "https://app.companycam.com/photos/8675309"


def test_photo_url_empty_id_returns_none() -> None:
    assert photo_url("") is None


def test_photo_url_blocks_non_numeric() -> None:
    assert photo_url("not-an-id") is None


# ---------------------------------------------------------------------------
# Target formatters
# ---------------------------------------------------------------------------


def test_project_target_uses_name() -> None:
    p = Project(id="94772883", name="Smith Residence")
    assert project_target(p) == "Smith Residence"


def test_project_target_falls_back_to_word_when_no_project() -> None:
    assert project_target(None) == "project"


def test_project_target_falls_back_to_word_when_name_empty() -> None:
    p = Project(id="94772883", name="")
    assert project_target(p) == "project"


def test_project_target_strips_newlines_from_name() -> None:
    """A malicious project name cannot forge a fake receipt line."""
    p = Project(id="94772883", name="Foo\nBar\nBaz")
    assert "\n" not in project_target(p)
    assert project_target(p) == "Foo Bar Baz"


def test_project_target_truncates_long_name() -> None:
    p = Project(id="x", name="A" * 200)
    out = project_target(p)
    assert len(out) == 60
    assert out.endswith("\u2026")


def test_photo_target_uses_description() -> None:
    ph = Photo(id="8675309", description="kitchen demo")
    assert photo_target(ph) == "kitchen demo"


def test_photo_target_falls_back_to_word() -> None:
    assert photo_target(None) == "photo"
    ph = Photo(id="8675309", description="")
    assert photo_target(ph) == "photo"


def test_photo_target_truncates_description() -> None:
    ph = Photo(id="x", description="A" * 200)
    out = photo_target(ph)
    assert len(out) == 60
    assert out.endswith("\u2026")


def test_photo_target_strips_newlines() -> None:
    ph = Photo(id="x", description="line1\nline2")
    assert "\n" not in photo_target(ph)


def test_comment_target_short() -> None:
    assert comment_target("All demo done") == "All demo done"


def test_comment_target_long_is_truncated_with_ellipsis() -> None:
    out = comment_target("A" * 100)
    assert len(out) == 40
    assert out.endswith("\u2026")


def test_comment_target_strips_newlines() -> None:
    """Receipt-injection defence: newline in content cannot forge a line."""
    assert "\n" not in comment_target("Hi\nFake - Receipt")
    assert comment_target("Hi\nFake receipt") == "Hi Fake receipt"


def test_comment_target_empty_falls_back_to_word() -> None:
    assert comment_target("") == "comment"
    assert comment_target("   \n\t   ") == "comment"


def test_tags_target_short_list() -> None:
    assert tags_target(["kitchen", "demo"]) == "kitchen, demo"


def test_tags_target_truncates_long_lists() -> None:
    assert tags_target(["a", "b", "c", "d", "e"]) == "a, b, c +2 more"


def test_tags_target_caps_single_tag_length() -> None:
    out = tags_target(["x" * 100])
    # One tag truncated to 25 chars with ellipsis.
    assert out.endswith("\u2026")
    assert len(out) == 25


def test_tags_target_empty_falls_back_to_word() -> None:
    assert tags_target([]) == "photo"
    assert tags_target([""]) == "photo"


# ---------------------------------------------------------------------------
# _sanitize internals
# ---------------------------------------------------------------------------


def test_sanitize_collapses_whitespace() -> None:
    assert _sanitize("foo \t  bar", 40) == "foo bar"


def test_sanitize_removes_control_chars() -> None:
    assert _sanitize("foo\x00bar\x07baz", 40) == "foo bar baz"


def test_sanitize_respects_length_cap() -> None:
    out = _sanitize("A" * 100, 10)
    assert len(out) == 10
    assert out.endswith("\u2026")


def test_sanitize_returns_empty_for_blank_input() -> None:
    assert _sanitize("", 10) == ""
    assert _sanitize("   ", 10) == ""
    assert _sanitize("\n\n\n", 10) == ""
