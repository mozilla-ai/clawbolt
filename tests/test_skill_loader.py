"""Tests for skill loader and skill instructions injection."""

from __future__ import annotations

import os

import pytest

from backend.app.agent.skills.loader import (
    _skill_instructions,
    get_skill_instructions,
    load_all_skills,
    load_skill_instructions,
)
from backend.app.agent.tools.base import ToolResult
from backend.app.agent.tools.registry import create_list_capabilities_tool

# ---------------------------------------------------------------------------
# load_skill_instructions
# ---------------------------------------------------------------------------


def test_load_skill_instructions_reads_skill_md() -> None:
    """load_skill_instructions should read SKILL.md from the given directory."""
    skill_dir = os.path.join(
        os.path.dirname(__file__),
        "..",
        "backend",
        "app",
        "integrations",
        "quickbooks",
    )
    content = load_skill_instructions(os.path.normpath(skill_dir))
    assert "QuickBooks" in content
    assert "qb_query" in content


def test_load_skill_instructions_missing_file(tmp_path: str) -> None:
    """load_skill_instructions should return empty string for missing SKILL.md."""
    content = load_skill_instructions(str(tmp_path))
    assert content == ""


# ---------------------------------------------------------------------------
# load_all_skills / get_skill_instructions
# ---------------------------------------------------------------------------


def test_load_all_skills_discovers_quickbooks() -> None:
    """load_all_skills should find the quickbooks skill package."""
    load_all_skills()
    assert "quickbooks" in _skill_instructions
    assert "qb_query" in _skill_instructions["quickbooks"]


def test_get_skill_instructions_returns_content() -> None:
    """get_skill_instructions should return SKILL.md content for known skills."""
    load_all_skills()
    content = get_skill_instructions("quickbooks")
    assert content is not None
    assert "QuickBooks" in content


def test_get_skill_instructions_returns_none_for_unknown() -> None:
    """get_skill_instructions should return None for unknown skill names."""
    assert get_skill_instructions("nonexistent_skill") is None


# ---------------------------------------------------------------------------
# list_capabilities integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_list_capabilities_includes_skill_instructions() -> None:
    """Activating a category with a SKILL.md should include instructions in the response."""
    load_all_skills()
    tool = create_list_capabilities_tool({"quickbooks": "QB tools"})
    result: ToolResult = await tool.function(category="quickbooks")
    assert result.is_error is False
    assert "activated" in result.content.lower()
    assert "QuickBooks" in result.content
    assert "qb_query" in result.content
    assert "Common Workflows" in result.content


@pytest.mark.asyncio()
async def test_list_capabilities_without_skill_instructions() -> None:
    """Activating a category without a SKILL.md should just show the activation message."""
    tool = create_list_capabilities_tool({"other_category": "Some tools"})
    result: ToolResult = await tool.function(category="other_category")
    assert result.is_error is False
    assert "activated" in result.content.lower()
    # Should not contain skill instructions since "other_category" has no SKILL.md
    assert "SKILL" not in result.content


@pytest.mark.asyncio()
async def test_list_capabilities_listing_unchanged() -> None:
    """Listing categories (no category arg) should work as before."""
    tool = create_list_capabilities_tool({"quickbooks": "QB tools", "files": "File tools"})
    result: ToolResult = await tool.function(category=None)
    assert result.is_error is False
    assert "quickbooks" in result.content
    assert "files" in result.content


@pytest.mark.asyncio()
async def test_list_capabilities_unknown_category() -> None:
    """Unknown categories should still return an error."""
    tool = create_list_capabilities_tool({"quickbooks": "QB tools"})
    result: ToolResult = await tool.function(category="nonexistent")
    assert result.is_error is True
    assert "Unknown category" in result.content
