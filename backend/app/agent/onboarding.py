"""Onboarding conversation logic for new users."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from backend.app.agent.events import AgentEndEvent, AgentEvent
from backend.app.agent.prompts import load_prompt
from backend.app.agent.tools.registry import default_registry, ensure_tool_modules_imported
from backend.app.config import settings
from backend.app.database import SessionLocal
from backend.app.models import User

if TYPE_CHECKING:
    from backend.app.agent.core import AgentResponse

logger = logging.getLogger(__name__)


def _bootstrap_path(user: User) -> Path:
    """Return the path to the user's BOOTSTRAP.md file."""
    return Path(settings.data_dir) / str(user.id) / "BOOTSTRAP.md"


def _user_dir(user: User) -> Path:
    """Return the user's data directory."""
    return Path(settings.data_dir) / str(user.id)


def _has_real_user_profile(user: User) -> bool:
    """Return True if user_text contains a filled-in name field.

    The default template has ``- Name:`` with no value. If the LLM has
    written a real name (e.g. ``- Name: Nathan``), the user has been
    through the onboarding conversation even if BOOTSTRAP.md was never
    deleted.
    """
    content = user.user_text or ""
    if not content:
        return False
    return bool(re.search(r"^-\s*Name:[ \t]+\S", content, re.MULTILINE))


def _has_custom_soul(user: User) -> bool:
    """Return True if soul_text differs from the default template."""
    content = (user.soul_text or "").strip()
    if not content:
        return False
    default = load_prompt("default_soul")
    default_wrapped = f"# Soul\n\n{default}"
    return content != default and content != default_wrapped


def is_onboarding_complete_heuristic(user: User) -> bool:
    """Heuristic check for onboarding completion.

    Returns True if there is evidence that the user has been through
    the onboarding conversation: USER.md has a real name, or SOUL.md
    has been customized from the default template.

    This catches the case where the LLM forgot to delete BOOTSTRAP.md
    after onboarding finished.
    """
    return _has_real_user_profile(user) or _has_custom_soul(user)


def is_onboarding_needed(user: User) -> bool:
    """Check if user needs onboarding.

    Returns False once onboarding_complete is set, or if BOOTSTRAP.md
    no longer exists in the user's directory, or if heuristic evidence
    shows the user has already completed onboarding.
    """
    if user.onboarding_complete:
        return False
    if not _bootstrap_path(user).exists():
        return False
    return not is_onboarding_complete_heuristic(user)


def _get_tool_capability_descriptions() -> list[str]:
    """Return human-readable descriptions of available tool capabilities.

    Uses the registry's specialist summaries so the onboarding prompt
    can tell the user what their assistant can do.
    """
    ensure_tool_modules_imported()
    summaries = default_registry.specialist_summaries
    return [f"- {name}: {summary}" for name, summary in sorted(summaries.items())]


def build_onboarding_system_prompt(
    user: User,
    tools: list[Any] | None = None,
) -> str:
    """Build system prompt for onboarding mode.

    Loads the user's BOOTSTRAP.md content and injects tool guidelines
    and behavioral instructions alongside it.  Earlier versions replaced
    the entire system prompt with just the bootstrap content, which
    stripped away communication instructions (e.g. "reply directly with
    text") and caused the model to return empty responses.
    """
    from backend.app.agent.system_prompt import (
        SystemPromptBuilder,
        build_date_section,
        build_instructions_section,
        build_tool_guidelines_section,
    )

    bootstrap = _bootstrap_path(user)
    if bootstrap.exists():
        base = bootstrap.read_text(encoding="utf-8").strip()
    else:
        base = load_prompt("bootstrap")

    # Inject available specialist capabilities into the bootstrap section
    capability_lines = _get_tool_capability_descriptions()
    if capability_lines:
        base += (
            "\n\nYour available specialist capabilities:\n"
            + "\n".join(capability_lines)
            + "\n\nMention the ones that seem relevant to the user's trade. "
            "Don't list them all at once."
        )

    base += (
        "\n\nIMPORTANT: If the user asks about something specific (a quote, a question, "
        "a photo), help them with that request FIRST, then naturally weave in any remaining "
        "onboarding questions. Never ignore their request just to collect profile info."
    )

    builder = SystemPromptBuilder()
    builder.set_preamble("You are an AI assistant for solo tradespeople.")
    builder.add_section("Onboarding", base)

    # Include tool guidelines and instructions so the model knows how
    # to communicate (reply with text, how to attach media, etc.).
    tool_guidelines = build_tool_guidelines_section(tools or [])
    instructions = build_instructions_section()
    if tool_guidelines:
        instructions += "\n\n## Tool Guidelines\n" + tool_guidelines
    builder.add_section("Instructions", instructions)
    builder.add_section("Current date", build_date_section(user))

    return builder.build()


class OnboardingSubscriber:
    """Event subscriber that detects onboarding completion after agent processing.

    Subscribes to ``AgentEndEvent`` to detect when the agent has deleted
    BOOTSTRAP.md (signaling onboarding is complete). When that happens,
    it sets ``onboarding_complete = True``.

    Usage::

        sub = OnboardingSubscriber(user, was_onboarding=True)
        agent.subscribe(sub)
        response = await agent.process_message(...)
        sub.finalize(response)
    """

    def __init__(self, user: User, was_onboarding: bool) -> None:
        self._user = user
        self._was_onboarding = was_onboarding

    async def __call__(self, event: AgentEvent) -> None:
        """Handle agent events. Only acts on ``AgentEndEvent``."""
        if isinstance(event, AgentEndEvent):
            await self._on_agent_end(event)

    async def _on_agent_end(self, event: AgentEndEvent) -> None:
        """Process onboarding state after the agent finishes."""
        if self._user.onboarding_complete:
            return

        # Transition: was onboarding and BOOTSTRAP.md is now gone
        if self._was_onboarding and not _bootstrap_path(self._user).exists():
            logger.info("Onboarding complete for user %s: BOOTSTRAP.md deleted", self._user.id)
            db = SessionLocal()
            try:
                db_user = db.query(User).filter_by(id=self._user.id).first()
                if db_user:
                    db_user.onboarding_complete = True
                    db.commit()
            finally:
                db.close()
            self._user.onboarding_complete = True
            return

        # Heuristic fallback: BOOTSTRAP.md still exists but user profile
        # shows evidence of completed onboarding (name filled in, or
        # soul_text customized).  This catches the case where the LLM got
        # sidetracked and forgot to delete BOOTSTRAP.md.
        # Re-read from DB since workspace tools may have updated text columns.
        if self._was_onboarding:
            db = SessionLocal()
            try:
                fresh = db.query(User).filter_by(id=self._user.id).first()
                if fresh:
                    self._user.user_text = fresh.user_text
                    self._user.soul_text = fresh.soul_text
            finally:
                db.close()
        if self._was_onboarding and is_onboarding_complete_heuristic(self._user):
            logger.info(
                "Onboarding complete for user %s: heuristic detected "
                "(BOOTSTRAP.md still exists, cleaning up)",
                self._user.id,
            )
            bootstrap = _bootstrap_path(self._user)
            if bootstrap.exists():
                bootstrap.unlink()
            db = SessionLocal()
            try:
                db_user = db.query(User).filter_by(id=self._user.id).first()
                if db_user:
                    db_user.onboarding_complete = True
                    db.commit()
            finally:
                db.close()
            self._user.onboarding_complete = True
            return

        # Pre-populated user: BOOTSTRAP.md doesn't exist but flag was never set
        if not is_onboarding_needed(self._user):
            logger.info(
                "Onboarding complete for user %s: pre-populated user",
                self._user.id,
            )
            db = SessionLocal()
            try:
                db_user = db.query(User).filter_by(id=self._user.id).first()
                if db_user:
                    db_user.onboarding_complete = True
                    db.commit()
            finally:
                db.close()
            self._user.onboarding_complete = True

    def finalize(self, response: AgentResponse) -> None:
        """No-op. Kept for API compatibility with the pipeline."""
