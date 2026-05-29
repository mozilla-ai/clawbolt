"""Doc-lint tests for the CompanyCam SKILL.md.

Regression for the "claimed a project did not exist without searching" bug:
the agent told a user it had no CompanyCam project for a name it had never
looked up this session, and offered to create one, instead of calling
``companycam_search_projects`` first. The project existed. The agent had been
reusing project IDs it resolved earlier in the session and generalized that
cache into a belief that its in-context set of projects was complete.

These tests pin the guard so the guidance does not drift out of the file.
"""

from __future__ import annotations

from pathlib import Path

SKILL_MD_PATH = (
    Path(__file__).resolve().parent.parent
    / "backend"
    / "app"
    / "integrations"
    / "companycam"
    / "SKILL.md"
)


def test_skill_md_has_finding_a_project_section() -> None:
    """SKILL.md must carry a dedicated "Finding a project" guard section."""
    content = SKILL_MD_PATH.read_text()
    assert "## Finding a project" in content, (
        "SKILL.md should include a 'Finding a project' section instructing the "
        "agent to search before claiming a project is absent."
    )


def test_skill_md_requires_search_before_claiming_absence() -> None:
    """The guard must tell the agent not to assert absence before searching.

    Without this, the agent answers from its in-context project cache: a name
    it has not searched this session reads as 'does not exist', so it offers to
    create a duplicate of a project that is already there.
    """
    content = SKILL_MD_PATH.read_text()
    lowered = content.lower()
    # The two tools the guard ties together.
    assert "companycam_search_projects" in content
    assert "companycam_create_project" in content
    # The core framing: not-searched is not the same as not-existing.
    assert "unknown, not absent" in lowered, (
        "The guard should state that an unsearched project is 'unknown, not "
        "absent' so the agent searches before claiming it does not exist."
    )
