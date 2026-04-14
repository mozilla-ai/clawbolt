"""Composable system prompt builder.

Replaces monolithic ``str.format()`` templates with a section-based builder
that safely concatenates user-supplied content without ``{``/``}`` injection
risks.  Both the main agent loop and the heartbeat engine use this builder.
"""

from __future__ import annotations

import datetime
import logging
import zoneinfo

from backend.app.agent.memory_db import build_memory_context
from backend.app.agent.prompts import load_prompt
from backend.app.agent.session_db import get_session_store
from backend.app.agent.tools.base import Tool
from backend.app.models import User

logger = logging.getLogger(__name__)


class SystemPromptBuilder:
    """Build a system prompt from composable sections.

    Each section has a heading and body.  ``build()`` assembles them
    into a single string with Markdown-style ``## Heading`` separators.
    No ``str.format()`` is used, so user-supplied content with curly
    braces is safe.

    Sections may be marked ``dynamic=True`` to indicate their content
    changes between calls (e.g. memory, cross-session context).
    ``build_content_blocks()`` uses this flag to split the prompt into
    a cacheable stable prefix and a non-cached dynamic suffix so that
    Anthropic prompt caching can reuse the stable prefix across turns.
    """

    def __init__(self) -> None:
        self._preamble: str = ""
        self._sections: list[tuple[str, str, bool]] = []  # (heading, content, dynamic)

    def set_preamble(self, text: str) -> SystemPromptBuilder:
        """Set the opening line(s) before any sections."""
        self._preamble = text
        return self

    def add_section(
        self,
        heading: str,
        content: str,
        *,
        dynamic: bool = False,
    ) -> SystemPromptBuilder:
        """Append a named section.  Empty content sections are skipped in output.

        Mark ``dynamic=True`` for sections whose content changes between
        turns (memory, cross-session context) so they are excluded from
        the prompt cache prefix.
        """
        self._sections.append((heading, content, dynamic))
        return self

    # Marker inserted between stable and dynamic sections so the caching
    # layer can split the prompt into a cacheable prefix and a dynamic suffix.
    CACHE_BOUNDARY = "\n<!-- CACHE_BOUNDARY -->\n"

    def build(self) -> str:
        """Assemble all sections into the final prompt string.

        A ``CACHE_BOUNDARY`` marker is inserted before the first dynamic
        section so ``prepare_system_with_caching()`` can split the prompt
        into a cacheable stable prefix and a non-cached dynamic suffix.
        """
        parts: list[str] = []
        if self._preamble:
            parts.append(self._preamble)

        hit_dynamic = False
        for heading, content, dynamic in self._sections:
            if not content:
                continue
            if dynamic and not hit_dynamic:
                hit_dynamic = True
                parts.append(self.CACHE_BOUNDARY.strip())
            parts.append(f"## {heading}\n{content}")

        return "\n\n".join(parts)


# -----------------------------------------------------------------------
# Reusable section builders
# -----------------------------------------------------------------------


def build_soul_prompt(user: User) -> str:
    """Build the 'soul' section of the system prompt from user profile.

    Returns the SOUL.md content directly. Identity info (name, personality)
    lives in the markdown, written by the agent during onboarding.
    """
    return user.soul_text or ""


def build_identity_section(user: User) -> str:
    """Build the 'About <name>' section content."""
    return build_soul_prompt(user)


def build_user_section(user: User) -> str:
    """Build the user profile section from USER.md content."""
    return user.user_text or ""


async def build_memory_section(
    user_id: str,
    query: str | None = None,
) -> str:
    """Build the memory context section content."""
    ctx = await build_memory_context(user_id)
    return ctx or "(No memories saved yet)"


def build_instructions_section() -> str:
    """Build the behavioral instructions section content.

    Trade-specific guidance is handled by the soul prompt (identity section),
    so this section only contains universal behavioral rules.
    """
    return load_prompt("instructions")


def build_tool_guidelines_section(tools: list[Tool]) -> str:
    """Build tool usage guidelines from registered tools."""
    hints = [tool.usage_hint for tool in tools if tool.usage_hint]
    if not hints:
        return ""
    return "\n".join(f"- {hint}" for hint in hints)


def build_proactive_section() -> str:
    """Build the proactive messaging rules section content."""
    return load_prompt("proactive")


def build_recall_section() -> str:
    """Build the recall behavior section content."""
    return load_prompt("recall")


def to_local_time(
    now: datetime.datetime,
    tz_name: str,
) -> datetime.datetime:
    """Convert *now* to the given IANA timezone, returning *now* unchanged on error."""
    if not tz_name:
        return now
    try:
        return now.astimezone(zoneinfo.ZoneInfo(tz_name))
    except (zoneinfo.ZoneInfoNotFoundError, KeyError, ValueError):
        logger.warning("Invalid timezone %r, falling back to UTC", tz_name)
        return now


def build_date_section(user: User) -> str:
    """Build a cache-friendly date string in the user's local timezone.

    Uses date-only granularity (no minutes) to avoid prompt-cache busting.
    """
    now = datetime.datetime.now(datetime.UTC)
    local = to_local_time(now, user.timezone)
    return local.strftime("%A, %Y-%m-%d")


def build_time_user_context(user: User) -> str:
    """Build a time context string to prepend to user messages.

    Moves the current time out of the system prompt (which breaks prompt
    caching) and into the user message where it is visible to the LLM but
    does not affect system prompt cache keys.
    """
    now = datetime.datetime.now(datetime.UTC)
    local = to_local_time(now, user.timezone)
    formatted = local.strftime("%A, %Y-%m-%d %I:%M %p").strip()
    if user.timezone:
        return f"[Current time: {formatted}]"
    return (
        f"[Current time: {formatted}. "
        "No timezone has been configured yet. "
        "If the user mentions their location or timezone, update USER.md "
        "with their timezone so future times are shown in their local time.]"
    )


def build_cross_session_context(
    user_id: str,
    current_session_id: str,
    count: int | None = None,
) -> str:
    """Build a summary of recent messages from other sessions.

    Gives the agent awareness of recent conversations that happened on
    a different channel (e.g. Telegram vs webchat) so it can maintain
    continuity when the user switches channels.
    """
    store = get_session_store(user_id)
    messages = store.get_other_session_messages(current_session_id, count=count)
    if not messages:
        return ""
    lines: list[str] = []
    for msg in messages:
        label = "You" if msg.direction == "outbound" else "User"
        body = msg.body[:200].rstrip()
        if len(msg.body) > 200:
            body += "..."
        lines.append(f"- [{label}] {body}")
    return (
        "These are your most recent messages from a different conversation session.\n"
        "Use this context for continuity but do not explicitly mention "
        '"another session" unless the user asks.\n\n' + "\n".join(lines)
    )


# -----------------------------------------------------------------------
# Pre-built prompt assemblers
# -----------------------------------------------------------------------


async def build_agent_system_prompt(
    user: User,
    tools: list[Tool],
    message_context: str,
    current_session_id: str = "",
) -> str:
    """Assemble the full system prompt for the main agent loop."""
    builder = SystemPromptBuilder()
    builder.set_preamble("You are an AI assistant for solo tradespeople.")

    builder.add_section(
        "About You",
        build_identity_section(user),
    )

    builder.add_section("About Your User", build_user_section(user))

    tool_guidelines = build_tool_guidelines_section(tools)
    if tool_guidelines:
        instructions = (
            build_instructions_section() + "\n" + "\n## Tool Guidelines\n" + tool_guidelines
        )
    else:
        instructions = build_instructions_section()
    builder.add_section("Instructions", instructions)

    builder.add_section("Proactive Messaging", build_proactive_section())
    builder.add_section("Recall Behavior", build_recall_section())

    # Dynamic sections: content changes between turns, placed after the
    # stable prefix so prompt caching can reuse the stable portion.
    memory = await build_memory_section(user.id, query=message_context)
    builder.add_section("Your Memory", memory, dynamic=True)

    if current_session_id:
        cross = build_cross_session_context(user.id, current_session_id)
        if cross:
            builder.add_section("Recent Activity (other channel)", cross, dynamic=True)

    return builder.build()


async def build_heartbeat_system_prompt(
    user: User,
    recent_messages: str,
    heartbeat_md: str = "",
    heartbeat_history: str = "",
) -> str:
    """Assemble the system prompt for the heartbeat evaluator.

    When *heartbeat_md* is provided, the raw HEARTBEAT.md content is
    included as a dedicated section so the LLM can evaluate which tasks
    need attention.  When *heartbeat_history* is provided, it shows when
    heartbeat messages were previously sent so the evaluator can reason
    about timing and avoid duplicates or missed sends.
    """
    builder = SystemPromptBuilder()
    builder.set_preamble(load_prompt("heartbeat_preamble"))

    builder.add_section("About You", build_identity_section(user))
    builder.add_section("About Your User", build_user_section(user))

    memory = await build_memory_section(user.id)
    builder.add_section("User's memory", memory)

    builder.add_section(
        "Recent conversation (last 5 messages)",
        recent_messages or "(no recent messages)",
    )

    builder.add_section(
        "User's heartbeat (HEARTBEAT.md)",
        heartbeat_md or "(no heartbeat items configured)",
    )

    if heartbeat_history:
        builder.add_section(
            "Recent heartbeat activity (timing reference only, not tasks to re-run)",
            heartbeat_history,
        )

    builder.add_section("Rules", load_prompt("heartbeat_rules"))

    return builder.build()
