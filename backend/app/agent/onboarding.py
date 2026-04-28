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

# Minimum number of *user* (inbound) messages before the heuristic fallback
# can fire. The bootstrap conversation needs at least this many user turns
# to plausibly cover name, timezone, trade context, and personality. Below
# this floor we trust that onboarding is still in progress regardless of
# what's been written to USER.md or SOUL.md. We count user messages, not
# total messages, because the agent can produce outbound messages without
# any new user information being captured.
MIN_ONBOARDING_USER_MESSAGES = 10

# Hard ceiling for force-completing onboarding. If the user has sent this
# many messages and onboarding has still not completed via either the
# BOOTSTRAP.md-deletion path or the heuristic+content path, force the
# flag on so heartbeats and other gated features unblock. This is a last-
# resort safety net for cases where the LLM never manages to satisfy any
# completion signal (e.g. user keeps redirecting to real questions, soul
# never gets customized). After 50 user turns the cost of staying stuck
# in onboarding outweighs the cost of an incomplete profile.
MAX_ONBOARDING_USER_MESSAGES = 50


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


def _has_user_timezone(user: User) -> bool:
    """Return True if user_text contains a filled-in Timezone field.

    The default template has ``- Timezone:`` with no value. Timezone is
    one of the two strictly-required fields per the bootstrap prompt
    (load-bearing for scheduling and heartbeat timing), so its presence
    is strong evidence that onboarding has progressed past the opening.
    """
    content = user.user_text or ""
    if not content:
        return False
    return bool(re.search(r"^-\s*Timezone:[ \t]+\S", content, re.MULTILINE))


def _has_custom_soul(user: User) -> bool:
    """Return True if soul_text differs from the default template."""
    content = (user.soul_text or "").strip()
    if not content:
        return False
    default = load_prompt("default_soul")
    default_wrapped = f"# Soul\n\n{default}"
    return content != default and content != default_wrapped


def is_onboarding_complete_heuristic(user: User) -> bool:
    """Heuristic gate for onboarding completion.

    Returns True only when ALL three evidence signals are present:
    USER.md has a real name, USER.md has a real timezone, and SOUL.md has
    been customized from the default template.

    The bootstrap prompt instructs the LLM to save the user's name as
    soon as it's heard (turn 2-3 of the conversation), so a name-only
    check fires far too early. Timezone is one of the two strictly-
    required fields per the prompt and is load-bearing for scheduling.
    SOUL.md customization happens near the end of onboarding once
    personality has been discussed. Requiring all three together gates
    the completion path on onboarding actually being substantively done.
    """
    return _has_real_user_profile(user) and _has_user_timezone(user) and _has_custom_soul(user)


def is_onboarding_needed(user: User) -> bool:
    """Check if user needs onboarding.

    Returns False once onboarding_complete is set, or if heuristic evidence
    shows the user has already completed onboarding (name and timezone in
    user_text plus a custom soul_text).

    Self-heal: if onboarding_complete is False and the heuristic shows no
    evidence of a prior onboarding, a missing BOOTSTRAP.md is re-written
    from the default template. This recovers from accidental deletions or
    a re-signup after an admin purge where the on-disk file was wiped but
    the user's onboarding flag remained False. Without this, a missing
    BOOTSTRAP.md would silently skip onboarding forever.
    """
    if user.onboarding_complete:
        logger.debug(
            "is_onboarding_needed(user=%s)=False: onboarding_complete flag set",
            user.id,
        )
        return False

    if is_onboarding_complete_heuristic(user):
        logger.debug(
            "is_onboarding_needed(user=%s)=False: heuristic detected existing profile "
            "(name_set=%s timezone_set=%s custom_soul=%s)",
            user.id,
            _has_real_user_profile(user),
            _has_user_timezone(user),
            _has_custom_soul(user),
        )
        return False

    bootstrap = _bootstrap_path(user)
    if not bootstrap.exists():
        try:
            bootstrap.parent.mkdir(parents=True, exist_ok=True)
            bootstrap.write_text(load_prompt("bootstrap") + "\n", encoding="utf-8")
            logger.warning(
                "is_onboarding_needed(user=%s): BOOTSTRAP.md was missing but user "
                "has no onboarding evidence; re-created from template. This usually "
                "means provision_user did not run (OAuth re-login after admin delete?) "
                "or the file was wiped from disk. Onboarding will proceed.",
                user.id,
            )
        except OSError:
            logger.exception(
                "is_onboarding_needed(user=%s): failed to re-create BOOTSTRAP.md; "
                "skipping onboarding this turn",
                user.id,
            )
            return False

    logger.debug(
        "is_onboarding_needed(user=%s)=True: onboarding_complete=False, "
        "no heuristic evidence, BOOTSTRAP.md present",
        user.id,
    )
    return True


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


def _mark_onboarding_complete(user: User) -> None:
    """Persist onboarding_complete=True for the user."""
    db = SessionLocal()
    try:
        db_user = db.query(User).filter_by(id=user.id).first()
        if db_user:
            db_user.onboarding_complete = True
            db.commit()
    finally:
        db.close()
    user.onboarding_complete = True


class OnboardingSubscriber:
    """Event subscriber that detects onboarding completion after agent processing.

    Four completion paths:

    1. **Primary:** the LLM deletes ``BOOTSTRAP.md`` per the bootstrap prompt.
       This is the intended signal and fires immediately.
    2. **Heuristic fallback:** if the LLM gets sidetracked and never deletes
       the file, the heuristic fires once the user has sent at least
       :data:`MIN_ONBOARDING_USER_MESSAGES` messages AND USER.md has a real
       name AND USER.md has a real timezone AND SOUL.md is customized.
       All gates are required: the bootstrap prompt instructs the LLM to
       save the user's name as soon as it's heard (turn 2-3), so name +
       short-conversation alone is not evidence that onboarding has
       substantively completed.
    3. **Hard ceiling:** at :data:`MAX_ONBOARDING_USER_MESSAGES` user
       messages, force-complete regardless of content. Last-resort safety
       net so heartbeats and other gated features don't stay disabled
       indefinitely when the LLM never satisfies any completion signal.
    4. **Pre-populated user:** users created with profile content already
       in place (migrations, admin seeding) get flipped on the first turn
       where they're not in onboarding mode.

    Usage::

        sub = OnboardingSubscriber(user, was_onboarding=True, user_message_count=N)
        agent.subscribe(sub)
        response = await agent.process_message(...)
        sub.finalize(response)
    """

    def __init__(
        self,
        user: User,
        was_onboarding: bool,
        user_message_count: int = 0,
    ) -> None:
        self._user = user
        self._was_onboarding = was_onboarding
        self._user_message_count = user_message_count

    async def __call__(self, event: AgentEvent) -> None:
        """Handle agent events. Only acts on ``AgentEndEvent``."""
        if isinstance(event, AgentEndEvent):
            await self._on_agent_end(event)

    async def _on_agent_end(self, event: AgentEndEvent) -> None:
        """Process onboarding state after the agent finishes."""
        if self._user.onboarding_complete:
            return

        # Path 1: BOOTSTRAP.md deletion (the intended signal).
        if self._was_onboarding and not _bootstrap_path(self._user).exists():
            logger.info("Onboarding complete for user %s: BOOTSTRAP.md deleted", self._user.id)
            _mark_onboarding_complete(self._user)
            return

        # Refresh user_text/soul_text from DB before evaluating the heuristic,
        # since workspace tools may have updated those columns in this turn.
        if self._was_onboarding:
            db = SessionLocal()
            try:
                fresh = db.query(User).filter_by(id=self._user.id).first()
                if fresh:
                    self._user.user_text = fresh.user_text
                    self._user.soul_text = fresh.soul_text
            finally:
                db.close()

        # Path 2: Heuristic fallback for sidetracked LLMs. All gates must
        # pass: we were in onboarding this turn, the user has sent enough
        # messages, name is set, timezone is set, and soul is customized.
        if (
            self._was_onboarding
            and self._user_message_count >= MIN_ONBOARDING_USER_MESSAGES
            and is_onboarding_complete_heuristic(self._user)
        ):
            logger.info(
                "Onboarding complete for user %s: heuristic detected "
                "(user_message_count=%d, BOOTSTRAP.md still exists, cleaning up)",
                self._user.id,
                self._user_message_count,
            )
            bootstrap = _bootstrap_path(self._user)
            if bootstrap.exists():
                bootstrap.unlink()
            _mark_onboarding_complete(self._user)
            return

        # Path 3: Hard ceiling. After enough user messages, force-complete
        # regardless of content so heartbeats and other gated features
        # don't stay disabled indefinitely. Logged at WARNING because this
        # path firing means the LLM never satisfied any completion signal
        # over a long conversation, which is worth investigating.
        if self._was_onboarding and self._user_message_count >= MAX_ONBOARDING_USER_MESSAGES:
            logger.warning(
                "Onboarding force-completed for user %s after %d user messages "
                "(name_set=%s timezone_set=%s custom_soul=%s, BOOTSTRAP.md still "
                "exists, cleaning up). The LLM never deleted BOOTSTRAP.md and "
                "never satisfied the heuristic gates over this conversation.",
                self._user.id,
                self._user_message_count,
                _has_real_user_profile(self._user),
                _has_user_timezone(self._user),
                _has_custom_soul(self._user),
            )
            bootstrap = _bootstrap_path(self._user)
            if bootstrap.exists():
                bootstrap.unlink()
            _mark_onboarding_complete(self._user)
            return

        # Path 4: pre-populated users. Not onboarding this turn but profile
        # text already shows positive evidence. Catches migrated users whose
        # flag was never flipped. A bare user with no profile data takes the
        # self-heal path in is_onboarding_needed instead.
        if not self._was_onboarding and is_onboarding_complete_heuristic(self._user):
            logger.info(
                "Onboarding complete for user %s: pre-populated profile detected "
                "(name_set=%s timezone_set=%s custom_soul=%s)",
                self._user.id,
                _has_real_user_profile(self._user),
                _has_user_timezone(self._user),
                _has_custom_soul(self._user),
            )
            _mark_onboarding_complete(self._user)

    def finalize(self, response: AgentResponse) -> None:
        """No-op. Kept for API compatibility with the pipeline."""
