"""Doc-lint tests for the AppFolio Vendor SKILL.md.

Regression for the "claimed a work order does not exist without searching"
bug (issue #1403): the agent told a user a work order was absent without
calling ``appfolio_search_work_orders`` first. Work orders are created
upstream by property managers, so there is no duplicate-create risk, but the
agent can still wrongly claim there is no work order for an address. The same
bug class as the CompanyCam fix in #1402 (project absence claimed without
searching).

These tests pin the guard so the guidance does not drift out of the file.
"""

from __future__ import annotations

from pathlib import Path

SKILL_MD_PATH = (
    Path(__file__).resolve().parent.parent
    / "backend"
    / "app"
    / "integrations"
    / "appfolio_vendor"
    / "SKILL.md"
)


def test_skill_md_has_finding_work_order_section() -> None:
    """SKILL.md must carry a dedicated 'Finding a work order' guard section."""
    content = SKILL_MD_PATH.read_text()
    assert "## Finding a work order" in content, (
        "SKILL.md should include a 'Finding a work order' section instructing "
        "the agent to search before claiming a work order is absent."
    )


def test_skill_md_requires_search_before_claiming_absence() -> None:
    """The guard must tell the agent not to assert absence before searching.

    Without this, the agent answers from its in-context work-order cache: an
    address it has not searched this session reads as 'does not exist', so it
    tells the user there is no work order.
    """
    content = SKILL_MD_PATH.read_text()
    lowered = content.lower()
    # The search tool the guard ties together.
    assert "appfolio_search_work_orders" in content
    # The core framing: not-searched is not the same as not-existing.
    assert "unknown, not absent" in lowered, (
        "The guard should state that an unsearched work order is 'unknown, not "
        "absent' so the agent searches before claiming it does not exist."
    )
