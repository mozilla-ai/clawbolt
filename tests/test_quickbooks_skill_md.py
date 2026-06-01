"""Doc-lint tests for the QuickBooks SKILL.md.

Regression for the "claimed an entity does not exist without querying" bug
(issue #1403): the agent told a user a customer, invoice, or estimate was
absent without calling ``qb_query`` first. The same bug class as issue #1131
(fields treated as absent because SKILL.md did not list them) and the
CompanyCam fix in #1402 (project absence claimed without searching).

These tests pin the guard so the guidance does not drift out of the file.
"""

from __future__ import annotations

from pathlib import Path

SKILL_MD_PATH = (
    Path(__file__).resolve().parent.parent
    / "backend"
    / "app"
    / "integrations"
    / "quickbooks"
    / "SKILL.md"
)


def test_skill_md_has_finding_entity_section() -> None:
    """SKILL.md must carry a dedicated 'Finding a customer, invoice, or estimate' guard section."""
    content = SKILL_MD_PATH.read_text()
    assert "## Finding a customer, invoice, or estimate" in content, (
        "SKILL.md should include a 'Finding a customer, invoice, or estimate' section "
        "instructing the agent to query before claiming an entity is absent."
    )


def test_skill_md_requires_query_before_claiming_absence() -> None:
    """The guard must tell the agent not to assert absence before querying.

    Without this, the agent answers from its in-context entity cache: a name
    it has not queried this session reads as 'does not exist', so it creates a
    duplicate of a customer that is already there, or claims an invoice does
    not exist.
    """
    content = SKILL_MD_PATH.read_text()
    lowered = content.lower()
    # The two tools the guard ties together.
    assert "qb_query" in content
    assert "qb_create" in content
    # The core framing: not-queried is not the same as not-existing.
    assert "unknown, not absent" in lowered, (
        "The guard should state that an unqueried entity is 'unknown, not "
        "absent' so the agent queries before claiming it does not exist."
    )


def test_skill_md_new_customer_job_queries_first() -> None:
    """The 'New customer job' workflow must query Customer before creating.

    The original workflow jumped straight to qb_create Customer, risking a
    duplicate. Reinforce that the agent searches first.
    """
    content = SKILL_MD_PATH.read_text()
    assert "qb_query" in content, (
        "The New customer job workflow should include a qb_query step to "
        "check if the customer exists before creating."
    )
