"""Composable system prompt builder.

Replaces monolithic ``str.format()`` templates with a section-based builder
that safely concatenates user-supplied content without ``{``/``}`` injection
risks.  Both the main agent loop and the heartbeat engine use this builder.
"""

from __future__ import annotations

import datetime
import logging
import zoneinfo

from backend.app.agent.file_store import UserData, get_session_store
from backend.app.agent.memory import build_memory_context
from backend.app.agent.profile import build_soul_prompt
from backend.app.agent.prompts import load_prompt
from backend.app.agent.tools.base import Tool

logger = logging.getLogger(__name__)

CONTEXT_QUERY_MAX_LENGTH = 100


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


def build_identity_section(user: UserData) -> str:
    """Build the 'About <name>' section content."""
    return build_soul_prompt(user)


def build_user_section(user: UserData) -> str:
    """Build the user profile section from USER.md content."""
    return user.user_text or ""


async def build_memory_section(
    user_id: int,
    query: str | None = None,
) -> str:
    """Build the memory context section content."""
    ctx = await build_memory_context(
        user_id,
        query=query[:CONTEXT_QUERY_MAX_LENGTH] if query else None,
    )
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


def _to_user_time(
    now: datetime.datetime,
    tz_name: str,
) -> datetime.datetime:
    """Convert *now* to the user's IANA timezone, falling back to UTC."""
    if not tz_name:
        return now
    try:
        return now.astimezone(zoneinfo.ZoneInfo(tz_name))
    except (zoneinfo.ZoneInfoNotFoundError, KeyError, ValueError):
        logger.warning("Invalid timezone %r, falling back to UTC", tz_name)
        return now


def build_date_section(user: UserData) -> str:
    """Build a cache-friendly date string in the user's local timezone.

    Uses date-only granularity (no minutes) to avoid prompt-cache busting.
    """
    now = datetime.datetime.now(datetime.UTC)
    local = _to_user_time(now, user.timezone)
    return local.strftime("%A, %Y-%m-%d")


def build_local_datetime_section(user: UserData) -> str:
    """Build a human-readable local datetime for the heartbeat evaluator."""
    now = datetime.datetime.now(datetime.UTC)
    local = _to_user_time(now, user.timezone)
    return local.strftime("%A, %Y-%m-%d %I:%M %p %Z").strip()


def build_cross_session_context(
    user_id: int,
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
    user: UserData,
    tools: list[Tool],
    message_context: str,
    current_session_id: str = "",
) -> str:
    """Assemble the full system prompt for the main agent loop."""
    builder = SystemPromptBuilder()
    assistant = user.assistant_name or "Clawbolt"
    builder.set_preamble(f"You are {assistant}, an AI assistant for solo tradespeople.")

    builder.add_section(
        f"About {user.name or 'User'}",
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

    builder.add_section("Current date", build_date_section(user))

    builder.add_section("Proactive Messaging", build_proactive_section())
    builder.add_section("Recall Behavior", build_recall_section())

    if current_session_id:
        cross = build_cross_session_context(user.id, current_session_id)
        if cross:
            builder.add_section("Recent Activity (other channel)", cross)

    return builder.build()


async def build_heartbeat_system_prompt(
    user: UserData,
    recent_messages: str,
    checklist_md: str = "",
) -> str:
    """Assemble the system prompt for the heartbeat evaluator.

    When *checklist_md* is provided, the raw HEARTBEAT.md content is
    included as a dedicated section so the LLM can evaluate which tasks
    need attention.
    """
    builder = SystemPromptBuilder()
    builder.set_preamble(load_prompt("heartbeat_preamble"))

    builder.add_section("About the user", build_identity_section(user))
    builder.add_section("About the user", build_user_section(user))

    memory = await build_memory_section(user.id)
    builder.add_section("User's memory", memory)

    builder.add_section(
        "Recent conversation (last 5 messages)",
        recent_messages or "(no recent messages)",
    )

    if checklist_md:
        builder.add_section("User's checklist (HEARTBEAT.md)", checklist_md)

    builder.add_section(
        "Current time",
        build_local_datetime_section(user),
    )

    builder.add_section("Rules", load_prompt("heartbeat_rules"))

    return builder.build()
