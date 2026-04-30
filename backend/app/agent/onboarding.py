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

# Floor for the system-driven auto-exit path. After this many user
# messages, if name + timezone are captured, the system removes
# BOOTSTRAP.md and marks onboarding complete without asking the LLM
# for a decision. The point of the floor is conversational texture:
# even a maximally-cooperative user who supplies name + timezone in
# turn 1 stays in bootstrap mode long enough for the "things worth
# weaving in" (dictation hint, photo policy) to land naturally.
# Calibrated to keep the user in onboarding for ~3-5 LLM replies.
MIN_USER_MESSAGES_FOR_AUTO_EXIT = 4

# Hard ceiling for force-completing onboarding. If the user has sent this
# many messages and onboarding has still not completed via the auto-exit
# path or the heuristic, force the flag on so heartbeats and other gated
# features unblock. Last-resort safety net for stuck flows.
MAX_ONBOARDING_USER_MESSAGES = 50


def _bootstrap_path(user: User) -> Path:
    """Return the path to the user's BOOTSTRAP.md file."""
    return Path(settings.data_dir) / str(user.id) / "BOOTSTRAP.md"


def _user_dir(user: User) -> Path:
    """Return the user's data directory."""
    return Path(settings.data_dir) / str(user.id)


def _has_real_user_profile(user: User) -> bool:
    """Return True if user_text contains a filled-in name field.

    The default template has ``- Name:`` with no value. The LLM is free
    to rewrite user_text and frequently picks a flat heading-style
    format (``Name: X``) instead of the bulleted default. Both shapes
    count as evidence that onboarding has progressed past the opening.
    """
    content = user.user_text or ""
    if not content:
        return False
    return bool(re.search(r"^[ \t]*-?[ \t]*Name:[ \t]+\S", content, re.MULTILINE))


def _has_user_timezone(user: User) -> bool:
    """Return True if the user has a populated timezone.

    Prefers the ``users.timezone`` column over text-grepping ``user_text``:
    the column is the authoritative source (set via ``PUT /user/profile``
    from the dashboard or browser onboarding flow), while user_text is
    a free-form summary the LLM may rewrite into any format. Timezone
    is load-bearing for scheduling and heartbeat timing, so its presence
    is strong evidence that onboarding has progressed past the opening.
    """
    return bool((user.timezone or "").strip())


def _has_custom_soul(user: User) -> bool:
    """Return True if soul_text differs from the default template."""
    content = (user.soul_text or "").strip()
    if not content:
        return False
    default = load_prompt("default_soul")
    default_wrapped = f"# Soul\n\n{default}"
    return content != default and content != default_wrapped


def is_ready_for_auto_exit(user: User) -> bool:
    """Return True when the system can flip the user out of bootstrap mode.

    Fires when both load-bearing fields (name + timezone) are populated.
    The conversation-length floor is enforced by the caller (see
    :data:`MIN_USER_MESSAGES_FOR_AUTO_EXIT`), so this function only
    captures the data condition.

    Distinct from :func:`is_onboarding_complete_heuristic`: that
    function is the strict heuristic backstop (also requires a custom
    soul + 10 messages) for users who somehow customized SOUL.md
    without exiting via the auto-exit path. The auto-exit path is the
    primary completion signal in the post-2026-04 bootstrap design,
    where the LLM no longer asks for personality and the system
    removes BOOTSTRAP.md once the user is established.
    """
    return _has_real_user_profile(user) and _has_user_timezone(user)


def is_onboarding_complete_heuristic(user: User) -> bool:
    """Heuristic gate for onboarding completion.

    Returns True only when ALL three evidence signals are present:
    USER.md has a real name, USER.md has a real timezone, and SOUL.md has
    been customized from the default template.

    Note on the soul gate after the 2026-04 bootstrap rewrite: the new
    bootstrap.md no longer asks the user to specify a personality, so
    the primary completion path is BOOTSTRAP.md deletion (path 1 in
    OnboardingSubscriber). This heuristic is now mainly a backstop for
    users whose conversation customized SOUL.md without the LLM
    deleting BOOTSTRAP.md. Keeping the soul gate prevents a silent
    auto-completion of mid-flight users who answered the old
    personality question yesterday and are replying today: their
    custom soul keeps them gated through this path until path 1 or
    path 3 (hard ceiling) fires.
    """
    return _has_real_user_profile(user) and _has_user_timezone(user) and _has_custom_soul(user)


def is_in_onboarding_flow(user: User) -> bool:
    """Side-effect-free check for whether a user is mid-onboarding.

    Returns True when the user has neither flipped the
    ``onboarding_complete`` flag nor accumulated heuristic evidence
    (real name + timezone + custom soul). Unlike
    :func:`is_onboarding_needed`, this does NOT touch disk: it never
    re-creates a missing BOOTSTRAP.md. Use this from read-only paths
    (preview endpoints, dashboards, anywhere a GET should be safe to
    repeat).

    Known divergence from :func:`is_onboarding_needed`: if BOOTSTRAP.md
    is missing AND the runtime fails to recreate it (rare OS-level
    error), :func:`is_onboarding_needed` returns ``False`` (drops
    onboarding for that turn) while this function still returns
    ``True``. Read-only callers will see ``is_onboarding=true`` for
    such a user even though the next runtime turn won't.
    """
    if user.onboarding_complete:
        return False
    return not is_onboarding_complete_heuristic(user)


def is_onboarding_needed(user: User) -> bool:
    """Check if user needs onboarding, self-healing on the way.

    Returns False once onboarding_complete is set, or if heuristic evidence
    shows the user has already completed onboarding (name and timezone in
    user_text plus a custom soul_text).

    Self-heal: if onboarding_complete is False and the heuristic shows no
    evidence of a prior onboarding, a missing BOOTSTRAP.md is re-written
    from the default template. This recovers from accidental deletions or
    a re-signup after an admin purge where the on-disk file was wiped but
    the user's onboarding flag remained False. Without this, a missing
    BOOTSTRAP.md would silently skip onboarding forever.

    Because this writes to disk, only call it from the inbound-message
    pipeline. Read-only / preview paths should use
    :func:`is_in_onboarding_flow` instead.
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
    base = load_prompt("bootstrap")
    if bootstrap.exists():
        try:
            base = bootstrap.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            logger.exception(
                "build_onboarding_system_prompt(user=%s): BOOTSTRAP.md exists but "
                "could not be read; falling back to default template",
                user.id,
            )

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

    Five completion paths, evaluated in order:

    1. **Defense-in-depth:** ``BOOTSTRAP.md`` is missing for any reason
       (LLM ran ``delete_file`` despite the prompt no longer asking, the
       file was wiped externally, etc.). Fires immediately.
    2. **Auto-exit (primary):** name + timezone captured AND the user has
       sent at least :data:`MIN_USER_MESSAGES_FOR_AUTO_EXIT` messages.
       The system removes ``BOOTSTRAP.md`` itself; the LLM has no
       exit-decision burden. The message floor ensures the conversation
       has texture (so the "things worth weaving in" content has a
       chance to land) even when the user supplies name + timezone
       in turn 1.
    3. **Strict heuristic backstop:** for mid-flight users from the
       pre-2026-04 bootstrap who customized SOUL.md (the old prompt
       asked them to). Fires once name + timezone + custom soul AND
       :data:`MIN_ONBOARDING_USER_MESSAGES` messages are present. After
       the new bootstrap is fully rolled out this path effectively
       disappears, since the new prompt does not ask for personality.
    4. **Hard ceiling:** at :data:`MAX_ONBOARDING_USER_MESSAGES` user
       messages, force-complete regardless of content. Last-resort
       safety net so heartbeats and other gated features don't stay
       disabled indefinitely when no completion signal fires.
    5. **Pre-populated user:** users created with profile content
       already in place (migrations, admin seeding) get flipped on the
       first turn where they're not in onboarding mode.

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

        # Path 1: BOOTSTRAP.md missing for any reason. Defense-in-depth
        # for cases where the LLM (or a workspace operation) removed the
        # file even though the prompt no longer asks the LLM to.
        if self._was_onboarding and not _bootstrap_path(self._user).exists():
            logger.info("Onboarding complete for user %s: BOOTSTRAP.md missing", self._user.id)
            _mark_onboarding_complete(self._user)
            return

        # Refresh user_text/soul_text from DB before evaluating the gates,
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

        # Path 2 (auto-exit, primary completion path in the post-2026-04
        # bootstrap design): name + timezone captured AND the user has
        # sent enough messages for the conversation to have texture. The
        # system removes BOOTSTRAP.md so the LLM has no exit-decision
        # burden and bootstrap-only guidance disappears from the system
        # prompt automatically once the user is established.
        if (
            self._was_onboarding
            and self._user_message_count >= MIN_USER_MESSAGES_FOR_AUTO_EXIT
            and is_ready_for_auto_exit(self._user)
        ):
            logger.info(
                "Onboarding auto-complete for user %s "
                "(user_message_count=%d, name+tz captured, removing BOOTSTRAP.md)",
                self._user.id,
                self._user_message_count,
            )
            bootstrap = _bootstrap_path(self._user)
            if bootstrap.exists():
                bootstrap.unlink()
            _mark_onboarding_complete(self._user)
            return

        # Path 3: Strict heuristic backstop. Fires for users who somehow
        # customized SOUL.md without exiting via path 2 (e.g. mid-flight
        # users from before the bootstrap rewrite who answered the old
        # personality question). Requires custom soul AND the long
        # message floor so we never cut off a real personality
        # conversation in progress.
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
