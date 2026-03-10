import logging

from backend.app.agent.file_store import UserData

logger = logging.getLogger(__name__)


def build_soul_prompt(user: UserData) -> str:
    """Build the 'soul' section of the system prompt from user profile.

    Returns the SOUL.md content directly. Identity info (name, personality)
    lives in the markdown, written by the agent during onboarding.
    """
    return user.soul_text or ""
