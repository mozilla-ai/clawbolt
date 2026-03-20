"""Composable system prompt builder.

Replaces monolithic ``str.format()`` templates with a section-based builder
that safely concatenates user-supplied content without ``{``/``}`` injection
risks.  Both the main agent loop and the heartbeat engine use this builder.
"""

from __future__ import annotations

import datetime
import logging
import zoneinfo

from backend.app.agent.memory import build_memory_context
from backend.app.agent.profile import build_soul_prompt
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
    """

    def __init__(self) -> None:
        self._preamble: str = ""
        self._sections: list[tuple[str, str]] = []

    def set_preamble(self, text: str) -> SystemPromptBuilder:
        """Set the opening line(s) before any sections."""
        self._preamble = text
        return self

    def add_section(self, heading: str, content: str) -> SystemPromptBuilder:
        """Append a named section.  Empty content sections are skipped in output."""
        self._sections.append((heading, content))
        return self

    def build(self) -> str:
        """Assemble all sections into the final prompt string."""
        parts: list[str] = []
        if self._preamble:
            parts.append(self._preamble)

        for heading, content in self._sections:
            if not content:
                continue
            parts.append(f"## {heading}\n{content}")

        return "\n\n".join(parts)


# -----------------------------------------------------------------------
# Reusable section builders
# -----------------------------------------------------------------------


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


def build_local_datetime_section(user: User) -> str:
    """Build a human-readable local datetime with explicit timezone context.

    Includes the IANA timezone name alongside the abbreviation so the LLM
    unambiguously knows which timezone the time is in, and a directive to
    always use this timezone when discussing times with the user.
    """
    now = datetime.datetime.now(datetime.UTC)
    local = to_local_time(now, user.timezone)
    formatted = local.strftime("%A, %Y-%m-%d %I:%M %p %Z").strip()
    if user.timezone:
        return (
            f"{formatted} ({user.timezone})\n"
            "This is the user's local time. Always use this timezone when "
            "discussing times, scheduling, or referring to deadlines."
        )
    return (
        f"{formatted}\n"
        "No timezone has been set for this user. This time is in UTC. "
        "If the user mentions their location or timezone, update USER.md "
        "with their timezone so future times are shown in their local time."
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

    memory = await build_memory_section(user.id, query=message_context)
    builder.add_section("Your Memory", memory)

    tool_guidelines = build_tool_guidelines_section(tools)
    if tool_guidelines:
        instructions = (
            build_instructions_section() + "\n" + "\n## Tool Guidelines\n" + tool_guidelines
        )
    else:
        instructions = build_instructions_section()
    builder.add_section("Instructions", instructions)

    builder.add_section("Current date and time", build_local_datetime_section(user))

    builder.add_section("Proactive Messaging", build_proactive_section())
    builder.add_section("Recall Behavior", build_recall_section())

    if current_session_id:
        cross = build_cross_session_context(user.id, current_session_id)
        if cross:
            builder.add_section("Recent Activity (other channel)", cross)

    return builder.build()


async def build_heartbeat_system_prompt(
    user: User,
    recent_messages: str,
    heartbeat_md: str = "",
) -> str:
    """Assemble the system prompt for the heartbeat evaluator.

    When *heartbeat_md* is provided, the raw HEARTBEAT.md content is
    included as a dedicated section so the LLM can evaluate which tasks
    need attention.
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

    if heartbeat_md:
        builder.add_section("User's heartbeat (HEARTBEAT.md)", heartbeat_md)

    builder.add_section(
        "Current time",
        build_local_datetime_section(user),
    )

    builder.add_section("Rules", load_prompt("heartbeat_rules"))

    return builder.build()
