"""Doc-lint tests for the file SKILL.md.

The file tools SKILL.md must guide the LLM on:
- When to use write_to_storage vs upload_to_storage (text from LLM vs attachment)
- When to use storage tools (Drive) vs workspace tools (MEMORY.md/USER.md/SOUL.md)
- The edit workflow: read first, then edit
"""

from __future__ import annotations

from pathlib import Path

SKILL_MD_PATH = (
    Path(__file__).resolve().parent.parent
    / "backend"
    / "app"
    / "agent"
    / "skills"
    / "file"
    / "SKILL.md"
)


def test_skill_md_has_writing_vs_uploading_section() -> None:
    """SKILL.md must explain when to use write_to_storage vs upload_to_storage."""
    content = SKILL_MD_PATH.read_text()
    assert "## Writing vs. Uploading" in content, (
        "SKILL.md should have a 'Writing vs. Uploading' section distinguishing "
        "write_to_storage (AI-generated text) from upload_to_storage (attachment)."
    )


def test_skill_md_mentions_both_write_and_upload_tools() -> None:
    """Both write_to_storage and upload_to_storage must be documented."""
    content = SKILL_MD_PATH.read_text()
    assert "write_to_storage" in content, "SKILL.md must mention write_to_storage."
    assert "upload_to_storage" in content, "SKILL.md must mention upload_to_storage."


def test_skill_md_has_storage_vs_workspace_section() -> None:
    """SKILL.md must explain when to use storage tools vs workspace tools."""
    content = SKILL_MD_PATH.read_text()
    assert "## When to use storage tools vs. workspace tools" in content, (
        "SKILL.md should have a section distinguishing storage (Drive) from "
        "workspace (MEMORY.md/USER.md/SOUL.md)."
    )


def test_skill_md_mentions_workspace_tools() -> None:
    """Workspace tools should be referenced so the LLM knows the boundary."""
    content = SKILL_MD_PATH.read_text()
    assert "read_file" in content, "SKILL.md must mention read_file (workspace) for comparison."
    assert "write_file" in content, "SKILL.md must mention write_file (workspace) for comparison."
    assert "MEMORY.md" in content or "USER.md" in content or "SOUL.md" in content, (
        "SKILL.md must reference at least one of MEMORY.md/USER.md/SOUL.md."
    )


def test_skill_md_describes_edit_workflow() -> None:
    """SKILL.md must tell the LLM to read before editing."""
    content = SKILL_MD_PATH.read_text()
    assert "## Editing a file" in content, "SKILL.md should have an 'Editing a file' section."
    assert "read_from_storage" in content, (
        "SKILL.md must mention read_from_storage in the edit workflow."
    )
    assert "old_text" in content, "SKILL.md must mention old_text to explain the edit mechanism."


def test_skill_md_has_connecting_section() -> None:
    """SKILL.md must tell the LLM how Google Drive gets connected."""
    content = SKILL_MD_PATH.read_text()
    assert "## Connecting" in content, "SKILL.md should have a 'Connecting' section."
    assert "manage_integration" in content, (
        "SKILL.md must mention manage_integration for Drive OAuth flow."
    )
