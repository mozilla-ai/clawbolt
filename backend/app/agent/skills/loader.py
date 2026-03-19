"""Skill loader: reads SKILL.md files from skill packages.

Skills are documentation-only packages that provide LLM-facing instructions
for a group of related tools. They are injected into the conversation context
when a specialist tool category is activated, following the OpenClaw pattern
of separating documentation (skills) from execution (tools).
"""

from __future__ import annotations

import importlib
import logging
import os
import pkgutil

logger = logging.getLogger(__name__)

# Mapping of factory name -> SKILL.md content, populated by load_all_skills().
_skill_instructions: dict[str, str] = {}


def load_skill_instructions(skill_dir: str) -> str:
    """Read SKILL.md from a skill's package directory.

    Returns empty string if the file is not found.
    """
    path = os.path.join(skill_dir, "SKILL.md")
    try:
        with open(path) as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def load_all_skills() -> None:
    """Discover all skill packages and load their SKILL.md content.

    Each sub-package under ``backend.app.agent.skills`` that contains a
    SKILL.md file is loaded. The package name (e.g. ``quickbooks``) is
    used as the key, matching the tool factory registration name.
    """
    package = importlib.import_module("backend.app.agent.skills")
    for _, name, is_pkg in pkgutil.iter_modules(package.__path__, package.__name__ + "."):
        if not is_pkg:
            continue
        mod = importlib.import_module(name)
        mod_dir = os.path.dirname(mod.__file__ or "")
        content = load_skill_instructions(mod_dir)
        if content:
            # Use the short package name (e.g. "quickbooks") as the key
            short_name = name.rsplit(".", 1)[-1]
            _skill_instructions[short_name] = content
            logger.debug("Loaded skill instructions for %r (%d chars)", short_name, len(content))


def get_skill_instructions(factory_name: str) -> str | None:
    """Return the SKILL.md content for a factory name, or None if not found."""
    return _skill_instructions.get(factory_name)
