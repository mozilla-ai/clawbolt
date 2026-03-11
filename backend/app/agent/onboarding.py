"""Onboarding conversation logic for new users."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from backend.app.agent.events import AgentEndEvent, AgentEvent
from backend.app.agent.file_store import UserData, get_user_store
from backend.app.agent.prompts import load_prompt
from backend.app.agent.tools.registry import default_registry, ensure_tool_modules_imported
from backend.app.config import settings

if TYPE_CHECKING:
    from backend.app.agent.core import AgentResponse

logger = logging.getLogger(__name__)


def _bootstrap_path(user: UserData) -> Path:
    """Return the path to the user's BOOTSTRAP.md file."""
    return Path(settings.data_dir) / str(user.id) / "BOOTSTRAP.md"


def is_onboarding_needed(user: UserData) -> bool:
    """Check if user needs onboarding.

    Returns False once onboarding_complete is set, or if BOOTSTRAP.md
    no longer exists in the user's directory.
    """
    if user.onboarding_complete:
        return False
    return _bootstrap_path(user).exists()


def _get_tool_capability_descriptions() -> list[str]:
    """Return human-readable descriptions of available tool capabilities.

    Uses the registry's specialist summaries so the onboarding prompt
    can tell the user what their assistant can do.
    """
    ensure_tool_modules_imported()
    summaries = default_registry.specialist_summaries
    return [f"- {name}: {summary}" for name, summary in sorted(summaries.items())]


def build_onboarding_system_prompt(
    user: UserData,
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
    # to communicate (reply with text, when to use send_reply, etc.).
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

    def __init__(self, user: UserData, was_onboarding: bool) -> None:
        self._user = user
        self._was_onboarding = was_onboarding

    async def __call__(self, event: AgentEvent) -> None:
        """Handle agent events. Only acts on ``AgentEndEvent``."""
        if isinstance(event, AgentEndEvent):
            await self._on_agent_end(event)

    async def _on_agent_end(self, event: AgentEndEvent) -> None:
        """Process onboarding state after the agent finishes."""
        store = get_user_store()

        # Transition: was onboarding and BOOTSTRAP.md is now gone
        if self._was_onboarding and not _bootstrap_path(self._user).exists():
            await store.update(self._user.id, onboarding_complete=True)
            self._user.onboarding_complete = True

        # Pre-populated user: BOOTSTRAP.md doesn't exist but flag was never set
        if not self._user.onboarding_complete and not is_onboarding_needed(self._user):
            await store.update(self._user.id, onboarding_complete=True)
            self._user.onboarding_complete = True

    def finalize(self, response: AgentResponse) -> None:
        """No-op. Kept for API compatibility with the pipeline."""
