"""Onboarding conversation logic for new users."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from backend.app.agent.events import AgentEndEvent, AgentEvent
from backend.app.agent.file_store import UserData, get_user_store
from backend.app.agent.profile import build_onboarding_prompt
from backend.app.agent.tools.names import ToolName
from backend.app.agent.tools.registry import default_registry, ensure_tool_modules_imported

if TYPE_CHECKING:
    from backend.app.agent.core import AgentResponse

logger = logging.getLogger(__name__)

# Fields that indicate a user has completed onboarding
REQUIRED_PROFILE_FIELDS = {"name"}


def is_onboarding_needed(user: UserData) -> bool:
    """Check if user needs onboarding.

    Returns False once onboarding_complete is set, or if the name
    field is already populated.
    """
    if user.onboarding_complete:
        return False
    return not user.name or not user.name.strip()


def _get_tool_capability_descriptions() -> list[str]:
    """Return human-readable descriptions of available tool capabilities.

    Uses the registry's specialist summaries so the onboarding prompt
    can tell the user what their assistant can do.
    """
    ensure_tool_modules_imported()
    summaries = default_registry.specialist_summaries
    return [f"- {name}: {summary}" for name, summary in sorted(summaries.items())]


def build_onboarding_system_prompt(user: UserData) -> str:
    """Build system prompt for onboarding mode.

    Wraps the base onboarding prompt with any partial profile info
    already collected so the agent doesn't re-ask known fields.
    Injects available tool capabilities so the agent can describe them.
    """
    base = build_onboarding_prompt()

    known: list[str] = []
    if user.name and user.name.strip():
        known.append(f"- Name: {user.name}")
    if user.assistant_name and user.assistant_name != "Clawbolt":
        known.append(f"- Your name (the AI): {user.assistant_name}")

    parts = [base]
    if known:
        parts.append("\n\nYou already know:\n" + "\n".join(known) + "\n\nDon't re-ask these.")

    # Inject available tool capabilities
    capability_lines = _get_tool_capability_descriptions()
    if capability_lines:
        parts.append(
            "\n\nYour available specialist capabilities:\n"
            + "\n".join(capability_lines)
            + "\n\nMention the ones that seem relevant to the user's trade. "
            "Don't list them all at once."
        )

    parts.append(
        "\n\nIMPORTANT: If the user asks about something specific (a quote, a question, "
        "a photo), help them with that request FIRST, then naturally weave in any remaining "
        "onboarding questions. Never ignore their request just to collect profile info."
    )

    return "".join(parts)


class OnboardingSubscriber:
    """Event subscriber that detects onboarding completion after agent processing.

    Subscribes to ``AgentEndEvent`` to detect successful ``update_profile`` calls.
    When the user's required profile fields become complete, it sets
    ``onboarding_complete = True`` and prepares a completion summary.

    Usage::

        sub = OnboardingSubscriber(user, was_onboarding=True)
        agent.subscribe(sub)
        response = await agent.process_message(...)
        sub.finalize(response)  # appends completion note to reply if applicable
    """

    def __init__(self, user: UserData, was_onboarding: bool) -> None:
        self._user = user
        self._was_onboarding = was_onboarding
        self._completion_note: str | None = None

    async def __call__(self, event: AgentEvent) -> None:
        """Handle agent events. Only acts on ``AgentEndEvent``."""
        if isinstance(event, AgentEndEvent):
            await self._on_agent_end(event)

    async def _on_agent_end(self, event: AgentEndEvent) -> None:
        """Process onboarding state after the agent finishes."""
        store = get_user_store()

        # Reload user if a profile update was made
        if any(a == f"Called {ToolName.UPDATE_PROFILE}" for a in event.actions_taken):
            refreshed = await store.get_by_id(self._user.id)
            if refreshed:
                self._user = refreshed

        # Transition: was onboarding and required fields are now complete
        if self._was_onboarding and not is_onboarding_needed(self._user):
            await store.update(self._user.id, onboarding_complete=True)
            self._user.onboarding_complete = True
            self._completion_note = self._build_completion_note()

        # Pre-populated user: fields were already filled but flag was never set
        if not self._user.onboarding_complete and not is_onboarding_needed(self._user):
            await store.update(self._user.id, onboarding_complete=True)
            self._user.onboarding_complete = True

    def _build_completion_note(self) -> str:
        assistant = self._user.assistant_name or "Clawbolt"
        parts = [f"Name: {self._user.name}"]
        parts.append(f"Your AI: {assistant}")
        summary = "\n".join(f"- {p}" for p in parts)
        return (
            "\n\nSetup complete! Here's what I know about you:\n"
            f"{summary}\n\n"
            "You can update any of this anytime. Now let's get to work."
        )

    def finalize(self, response: AgentResponse) -> None:
        """Append the completion note to the response if onboarding just completed."""
        if self._completion_note and response.reply_text:
            response.reply_text += self._completion_note
