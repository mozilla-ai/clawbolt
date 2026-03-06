"""Onboarding conversation logic for new contractors."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from backend.app.agent.events import AgentEndEvent, AgentEvent
from backend.app.agent.file_store import ContractorData, get_contractor_store
from backend.app.agent.profile import build_onboarding_prompt
from backend.app.agent.tools.names import ToolName

if TYPE_CHECKING:
    from backend.app.agent.core import AgentResponse

logger = logging.getLogger(__name__)

# Fields that indicate a contractor has completed onboarding
REQUIRED_PROFILE_FIELDS = {"name", "trade", "location"}


def is_onboarding_needed(contractor: ContractorData) -> bool:
    """Check if contractor needs onboarding.

    Returns False once onboarding_complete is set, or if all required
    profile fields are already populated.
    """
    if contractor.onboarding_complete:
        return False
    for field in REQUIRED_PROFILE_FIELDS:
        value = getattr(contractor, field, None)
        if not value or not str(value).strip():
            return True
    return False


def build_onboarding_system_prompt(contractor: ContractorData) -> str:
    """Build system prompt for onboarding mode.

    Wraps the base onboarding prompt with any partial profile info
    already collected so the agent doesn't re-ask known fields.
    """
    base = build_onboarding_prompt()

    known: list[str] = []
    if contractor.name and contractor.name.strip():
        known.append(f"- Name: {contractor.name}")
    if contractor.trade and contractor.trade.strip():
        known.append(f"- Trade: {contractor.trade}")
    if contractor.location and contractor.location.strip():
        known.append(f"- Location: {contractor.location}")
    if contractor.assistant_name and contractor.assistant_name != "Clawbolt":
        known.append(f"- Your name (the AI): {contractor.assistant_name}")
    if contractor.hourly_rate:
        known.append(f"- Rate: ${contractor.hourly_rate:.0f}/hour")
    if contractor.business_hours and contractor.business_hours.strip():
        known.append(f"- Business hours: {contractor.business_hours}")
    if contractor.timezone and contractor.timezone.strip():
        known.append(f"- Timezone: {contractor.timezone}")
    if contractor.preferences_json and contractor.preferences_json != "{}":
        try:
            prefs = json.loads(contractor.preferences_json)
            if isinstance(prefs, dict):
                style = prefs.get("communication_style")
                if style:
                    known.append(f"- Communication style: {style}")
        except (json.JSONDecodeError, TypeError):
            pass

    parts = [base]
    if known:
        parts.append("\n\nYou already know:\n" + "\n".join(known) + "\n\nDon't re-ask these.")

    parts.append(
        "\n\nIMPORTANT: If the contractor asks about something specific (a quote, a question, "
        "a photo), help them with that request FIRST, then naturally weave in any remaining "
        "onboarding questions. Never ignore their request just to collect profile info."
    )

    return "".join(parts)


class OnboardingSubscriber:
    """Event subscriber that detects onboarding completion after agent processing.

    Subscribes to ``AgentEndEvent`` to detect successful ``update_profile`` calls.
    When the contractor's required profile fields become complete, it sets
    ``onboarding_complete = True`` and prepares a completion summary.

    Usage::

        sub = OnboardingSubscriber(contractor, was_onboarding=True)
        agent.subscribe(sub)
        response = await agent.process_message(...)
        sub.finalize(response)  # appends completion note to reply if applicable
    """

    def __init__(self, contractor: ContractorData, was_onboarding: bool) -> None:
        self._contractor = contractor
        self._was_onboarding = was_onboarding
        self._completion_note: str | None = None

    async def __call__(self, event: AgentEvent) -> None:
        """Handle agent events. Only acts on ``AgentEndEvent``."""
        if isinstance(event, AgentEndEvent):
            await self._on_agent_end(event)

    async def _on_agent_end(self, event: AgentEndEvent) -> None:
        """Process onboarding state after the agent finishes."""
        store = get_contractor_store()

        # Reload contractor if a profile update was made
        if any(a == f"Called {ToolName.UPDATE_PROFILE}" for a in event.actions_taken):
            refreshed = await store.get_by_id(self._contractor.id)
            if refreshed:
                self._contractor = refreshed

        # Transition: was onboarding and required fields are now complete
        if self._was_onboarding and not is_onboarding_needed(self._contractor):
            await store.update(self._contractor.id, onboarding_complete=True)
            self._contractor.onboarding_complete = True
            self._completion_note = self._build_completion_note()

        # Pre-populated contractor: fields were already filled but flag was never set
        if not self._contractor.onboarding_complete and not is_onboarding_needed(self._contractor):
            await store.update(self._contractor.id, onboarding_complete=True)
            self._contractor.onboarding_complete = True

    def _build_completion_note(self) -> str:
        assistant = self._contractor.assistant_name or "Clawbolt"
        parts = [f"Name: {self._contractor.name}", f"Trade: {self._contractor.trade}"]
        if self._contractor.location:
            parts.append(f"Location: {self._contractor.location}")
        if self._contractor.hourly_rate:
            parts.append(f"Rate: ${self._contractor.hourly_rate:.0f}/hour")
        parts.append(f"Your AI: {assistant}")
        summary = "\n".join(f"- {p}" for p in parts)
        return (
            "\n\nSetup complete! Here's what I know about you:\n"
            f"{summary}\n\n"
            "You can update any of this anytime. I'm ready to help!"
        )

    def finalize(self, response: AgentResponse) -> None:
        """Append the completion note to the response if onboarding just completed."""
        if self._completion_note and response.reply_text:
            response.reply_text += self._completion_note
