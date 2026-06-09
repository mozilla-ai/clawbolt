"""Composable system prompt builder.

Replaces monolithic ``str.format()`` templates with a section-based builder
that safely concatenates user-supplied content without ``{``/``}`` injection
risks.  Both the main agent loop and the heartbeat engine use this builder.
"""

from __future__ import annotations

import datetime
import logging
import zoneinfo

from backend.app.agent.markdown_registry import truncate_for_injection
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
    ``build_parts()`` uses this flag to separate the cacheable stable
    sections from the dynamic ones. The agent loop sends the stable half
    in the ``system`` param (cacheable) and emits the dynamic half after
    the message history so a memory write no longer invalidates the
    history cache (see #1420).
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

    def build_parts(self) -> tuple[str, str]:
        """Return the ``(stable, dynamic)`` halves of the prompt.

        *stable* is the preamble plus every non-dynamic section, joined
        with blank lines. It is byte-stable across turns and is sent in
        the cacheable ``system`` param.

        *dynamic* is every section marked ``dynamic=True`` (memory,
        integration status, cross-session context). It changes between
        turns and is emitted outside the ``system`` param by the agent
        loop so it does not gate the message-history cache.

        Section order within each half is preserved.
        """
        stable_parts: list[str] = []
        dynamic_parts: list[str] = []
        if self._preamble:
            stable_parts.append(self._preamble)

        for heading, content, dynamic in self._sections:
            if not content:
                continue
            target = dynamic_parts if dynamic else stable_parts
            target.append(f"## {heading}\n{content}")

        return "\n\n".join(stable_parts), "\n\n".join(dynamic_parts)

    def build(self) -> str:
        """Assemble all sections into a single prompt string.

        Stable sections come first, then dynamic ones. Used by callers
        that want the full prompt as one string (e.g. the system-prompt
        preview endpoint and prompts with no dynamic sections). The agent
        loop uses :meth:`build_parts` instead so it can place the dynamic
        half after the history.
        """
        stable, dynamic = self.build_parts()
        if stable and dynamic:
            return f"{stable}\n\n{dynamic}"
        return stable or dynamic


# -----------------------------------------------------------------------
# Reusable section builders
# -----------------------------------------------------------------------


def build_soul_prompt(user: User) -> str:
    """Build the 'soul' section of the system prompt from user profile.

    Returns the SOUL.md content directly. Identity info (name, personality)
    lives in the markdown, written by the agent during onboarding.

    Tail-truncates over-budget rows via
    :func:`backend.app.agent.markdown_registry.truncate_for_injection`
    so a row that pre-dates the write-time cap (or that was migrated
    in by an earlier release) cannot bloat every system prompt.
    """
    return truncate_for_injection("SOUL.md", user.soul_text or "")


def build_identity_section(user: User) -> str:
    """Build the 'About <name>' section content."""
    return build_soul_prompt(user)


def build_user_section(user: User) -> str:
    """Build the user profile section from USER.md content.

    Tail-truncates over-budget rows for prompt injection; see
    :func:`build_soul_prompt` for the rationale.

    Strips any agent-authored ``## Integrations`` (or ``# Integrations``)
    subsection before injection. Connection state lives in the live
    "Connected Integrations" section built from ``oauth_service``; a
    stale copy in USER.md previously caused the agent to declare
    "Drive isn't connected" without checking, because USER.md had been
    written before the user OAuthed. The strip is defensive: the
    Instructions section now tells the agent not to write that block,
    but legacy users still have it in their stored USER.md.
    """
    user_text = _strip_integrations_block(user.user_text or "")
    return truncate_for_injection("USER.md", user_text)


def _strip_integrations_block(text: str) -> str:
    """Remove a ``# Integrations`` or ``## Integrations`` subsection from *text*.

    Matches the heading literally and everything up to the next heading at
    the same or shallower depth (or end of file).
    """
    if not text or "Integrations" not in text:
        return text
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    skipping = False
    skip_depth = 0
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("#"):
            depth = len(stripped) - len(stripped.lstrip("#"))
            heading_text = stripped[depth:].strip()
            if not skipping and heading_text.lower() == "integrations":
                skipping = True
                skip_depth = depth
                continue
            if skipping and depth <= skip_depth:
                skipping = False
        if not skipping:
            out.append(line)
    result = "".join(out)
    # Drop trailing blank lines that the stripped block left behind, but
    # preserve a single trailing newline if the original had one.
    trailing_nl = "\n" if text.endswith("\n") else ""
    return result.rstrip() + trailing_nl


async def build_integration_status_section(user_id: str) -> str:
    """Build the 'Connected Integrations' section content (live state).

    Queried fresh on every system-prompt build from ``oauth_service`` so a
    user's OAuth completion mid-conversation reflects on the very next
    turn. The model is told (in ``instructions.md``) to treat this section
    as authoritative over anything written into USER.md or MEMORY.md.

    Integrations the operator has not configured on this deployment are
    omitted entirely; the model only sees integrations it could plausibly
    ask the user to connect.
    """
    # Local import: avoids a circular dependency at module-import time
    # (``integration_tools`` imports several agent helpers that may
    # eventually grow to import ``system_prompt``).
    from backend.app.agent.tools.integration_tools import (
        get_user_connected_integrations,
    )

    status = await get_user_connected_integrations(user_id)
    if not status:
        return ""

    connected = sorted(name for name, ok in status.items() if ok)
    not_connected = sorted(name for name, ok in status.items() if not ok)

    lines: list[str] = [
        "Live connection state. Authoritative over anything in USER.md or MEMORY.md.",
    ]
    if connected:
        lines.append(f"Connected: {', '.join(connected)}")
    else:
        lines.append("Connected: (none)")
    if not_connected:
        lines.append(f"Not connected: {', '.join(not_connected)}")
    return "\n".join(lines)


async def build_memory_section(
    user_id: str,
    query: str | None = None,
) -> str:
    """Build the memory context section content.

    Tail-truncates over-budget rows for prompt injection; see
    :func:`build_soul_prompt` for the rationale.
    """
    ctx = await build_memory_context(user_id)
    if not ctx:
        return "(No memories saved yet)"
    return truncate_for_injection("MEMORY.md", ctx)


def build_instructions_section() -> str:
    """Build the behavioral instructions section content.

    Trade-specific guidance is handled by the soul prompt (identity section),
    so this section only contains universal behavioral rules.
    """
    body = load_prompt("instructions")
    body += (
        "\n\n## Media handling\n"
        "When the user sends a photo, the attachment appears in your context"
        " with a handle like `media_ab12cd`. Default: do not analyze the"
        " photo. Use analyze_photo only when the user has asked you to"
        " look at the image, or you genuinely need to see its contents"
        " to help. The agent has separate tools for storing, attaching,"
        " and discarding photos; pick the right one based on what the"
        " user asked for. Skipping all media tools on a photo is fine"
        " when the user did not ask for anything file-related."
    )
    return body


def build_tool_guidelines_section(tools: list[Tool]) -> str:
    """Build tool usage guidelines from registered tools."""
    hints = [tool.usage_hint for tool in tools if tool.usage_hint]
    if not hints:
        return ""
    return "\n".join(f"- {hint}" for hint in hints)


def build_proactive_section() -> str:
    """Build the proactive messaging rules section content."""
    return load_prompt("proactive")


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
        return f"[Current time: {formatted} ({user.timezone})]"
    return (
        f"[Current time: {formatted} (UTC). "
        "No timezone has been configured yet. "
        "If the user mentions their location or timezone, update USER.md "
        "with their timezone so future times are shown in their local time.]"
    )


async def build_cross_session_context(
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
    messages = await store.get_other_session_messages_async(current_session_id, count=count)
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


async def _build_agent_prompt_builder(
    user: User,
    tools: list[Tool],
    message_context: str,
    current_session_id: str = "",
) -> SystemPromptBuilder:
    """Assemble the composable builder for the main agent loop.

    Shared by :func:`build_agent_system_prompt` (full string, used by the
    preview endpoint) and :func:`build_agent_system_prompt_parts` (stable
    and dynamic halves, used by the agent loop).
    """
    builder = SystemPromptBuilder()
    builder.set_preamble("You are an AI assistant for solo tradespeople.")

    builder.add_section(
        "About You",
        build_identity_section(user),
    )

    builder.add_section("About Your User", build_user_section(user))

    builder.add_section("Instructions", build_instructions_section())

    builder.add_section("Proactive Messaging", build_proactive_section())

    # Dynamic sections: content changes between turns, placed after the
    # stable prefix so prompt caching can reuse the stable portion. Tool
    # guidelines are dynamic because newly activated specialists append
    # their usage hints mid-conversation. Keeping them out of Instructions
    # prevents that activation from busting the stable system-prompt cache.
    tool_guidelines = build_tool_guidelines_section(tools)
    if tool_guidelines:
        builder.add_section("Tool Guidelines", tool_guidelines, dynamic=True)

    # Live integration state is dynamic: a user can complete an OAuth
    # handshake mid-conversation and we want the next turn to reflect it
    # without depending on the agent to have written it into USER.md.
    integration_status = await build_integration_status_section(user.id)
    if integration_status:
        builder.add_section("Connected Integrations", integration_status, dynamic=True)

    memory = await build_memory_section(user.id, query=message_context)
    builder.add_section("Your Memory", memory, dynamic=True)

    if current_session_id:
        cross = await build_cross_session_context(user.id, current_session_id)
        if cross:
            builder.add_section("Recent Activity (other channel)", cross, dynamic=True)

    return builder


async def build_agent_system_prompt(
    user: User,
    tools: list[Tool],
    message_context: str,
    current_session_id: str = "",
) -> str:
    """Assemble the full system prompt for the main agent loop.

    Returns the stable and dynamic sections as one string. Used by the
    system-prompt preview endpoint; the agent loop uses
    :func:`build_agent_system_prompt_parts` instead.
    """
    builder = await _build_agent_prompt_builder(user, tools, message_context, current_session_id)
    return builder.build()


async def build_agent_system_prompt_parts(
    user: User,
    tools: list[Tool],
    message_context: str,
    current_session_id: str = "",
) -> tuple[str, str]:
    """Assemble the agent system prompt as ``(stable, dynamic)`` halves.

    The agent loop sends *stable* in the cacheable ``system`` param and
    appends *dynamic* (memory, integration status, cross-session context)
    to the current user turn, so a memory write does not invalidate the
    message-history prompt cache (#1420).
    """
    builder = await _build_agent_prompt_builder(user, tools, message_context, current_session_id)
    return builder.build_parts()


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

    integration_status = await build_integration_status_section(user.id)
    if integration_status:
        builder.add_section("Connected Integrations", integration_status)

    memory = await build_memory_section(user.id)
    builder.add_section("User's memory", memory)

    builder.add_section(
        "Recent conversation (last 5 messages)",
        recent_messages or "(no recent messages)",
    )

    builder.add_section(
        "User's heartbeat (HEARTBEAT.md)",
        truncate_for_injection("HEARTBEAT.md", heartbeat_md)
        if heartbeat_md
        else "(no heartbeat items configured)",
    )

    if heartbeat_history:
        builder.add_section(
            "Recent heartbeat activity (timing reference only, not tasks to re-run)",
            heartbeat_history,
        )

    builder.add_section("Rules", load_prompt("heartbeat_rules"))

    return builder.build()
