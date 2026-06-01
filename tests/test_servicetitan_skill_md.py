"""Doc-lint tests for the ServiceTitan SKILL.md.

Regression for the "claimed a customer or job does not exist without
searching" bug (issue #1403): the agent told a user a customer or job was
absent without calling ``st_search_customers`` first. ServiceTitan is
read-only, so there is no duplicate-create risk, but the agent can still
falsely tell the user an entity does not exist and fail to target
``st_add_job_note``. The same bug class as the CompanyCam fix in #1402
(project absence claimed without searching).

These tests pin the guard so the guidance does not drift out of the file.
"""

from __future__ import annotations

from pathlib import Path

SKILL_MD_PATH = (
    Path(__file__).resolve().parent.parent
    / "backend"
    / "app"
    / "integrations"
    / "servicetitan"
    / "SKILL.md"
)


def test_skill_md_has_finding_customer_section() -> None:
    """SKILL.md must carry a dedicated 'Finding a customer or job' guard section."""
    content = SKILL_MD_PATH.read_text()
    assert "## Finding a customer or job" in content, (
        "SKILL.md should include a 'Finding a customer or job' section "
        "instructing the agent to search before claiming a customer or job is "
        "absent."
    )


def test_skill_md_requires_search_before_claiming_absence() -> None:
    """The guard must tell the agent not to assert absence before searching.

    Without this, the agent answers from its in-context customer cache: a name
    it has not searched this session reads as 'does not exist', so it tells
    the user there is no such customer and fails to add a job note.
    """
    content = SKILL_MD_PATH.read_text()
    lowered = content.lower()
    # The search tool the guard ties together.
    assert "st_search_customers" in content
    # The core framing: not-searched is not the same as not-existing.
    assert "unknown, not absent" in lowered, (
        "The guard should state that an unsearched customer or job is "
        "'unknown, not absent' so the agent searches before claiming it does "
        "not exist."
    )
