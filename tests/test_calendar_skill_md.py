"""Doc-lint tests for the Calendar SKILL.md.

Regression for the "claimed an event does not exist without listing" bug
(issue #1403): the agent told a user an event was absent without calling
``calendar_list_events`` first, or created a duplicate event because it did
not check existing events. The same bug class as the CompanyCam fix in #1402
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
    / "calendar"
    / "SKILL.md"
)


def test_skill_md_has_finding_event_section() -> None:
    """SKILL.md must carry a dedicated 'Finding an event' guard section."""
    content = SKILL_MD_PATH.read_text()
    assert "## Finding an event" in content, (
        "SKILL.md should include a 'Finding an event' section instructing the "
        "agent to list events before claiming an event is absent."
    )


def test_skill_md_requires_list_before_claiming_absence() -> None:
    """The guard must tell the agent not to assert absence before listing events.

    Without this, the agent answers from its in-context event cache: an event
    it has not listed this session reads as 'does not exist', so it creates a
    duplicate or claims an event to update/delete is absent.
    """
    content = SKILL_MD_PATH.read_text()
    lowered = content.lower()
    # The two tools the guard ties together.
    assert "calendar_list_events" in content
    assert "calendar_create_event" in content
    # The core framing: not-listed is not the same as not-existing.
    assert "unknown, not absent" in lowered, (
        "The guard should state that an unlisted event is 'unknown, not "
        "absent' so the agent lists events before claiming it does not exist."
    )
