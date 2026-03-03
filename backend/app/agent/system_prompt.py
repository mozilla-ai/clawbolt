"""Composable system prompt builder.

Replaces monolithic ``str.format()`` templates with a section-based builder
that safely concatenates user-supplied content without ``{``/``}`` injection
risks.  Both the main agent loop and the heartbeat engine use this builder.
"""

from __future__ import annotations

import datetime
import logging

from sqlalchemy.orm import Session

from backend.app.agent.memory import build_memory_context
from backend.app.agent.profile import build_soul_prompt, get_missing_optional_fields
from backend.app.agent.tools.base import Tool
from backend.app.models import Contractor

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


def build_identity_section(contractor: Contractor) -> str:
    """Build the 'About <name>' section content."""
    return build_soul_prompt(contractor)


async def build_memory_section(
    db: Session,
    contractor_id: int,
    query: str | None = None,
) -> str:
    """Build the memory context section content."""
    ctx = await build_memory_context(
        db,
        contractor_id,
        query=query[:CONTEXT_QUERY_MAX_LENGTH] if query else None,
    )
    return ctx or "(No memories saved yet)"


def build_instructions_section() -> str:
    """Build the behavioral instructions section content."""
    return (
        "- Be concise and practical. Contractors are busy.\n"
        "- You can ONLY communicate via this chat. You cannot send emails, "
        "make phone calls, or contact clients directly.\n"
        "- Always be helpful, friendly, and professional.\n"
        "- Keep replies concise. Contractors are on the job site."
    )


def build_tool_guidelines_section(tools: list[Tool]) -> str:
    """Build tool usage guidelines from registered tools."""
    hints = [tool.usage_hint for tool in tools if tool.usage_hint]
    if not hints:
        return ""
    return "\n".join(f"- {hint}" for hint in hints)


def build_proactive_section() -> str:
    """Build the proactive messaging rules section content."""
    return (
        "You will proactively reach out during business hours when something needs attention:\n"
        "- A draft estimate has been sitting unsent for over 24 hours\n"
        "- A scheduled checklist item is due\n"
        "- A follow-up reminder or deadline is approaching\n"
        "- You haven't heard from the contractor in a few days"
    )


def build_recall_section() -> str:
    """Build the recall behavior section content."""
    return (
        "When the contractor asks a question about their business, clients, or past work:\n"
        "1. Search your memory for relevant information.\n"
        "2. If you find relevant facts, use them to answer clearly and concisely.\n"
        "3. If you don't find anything, say so honestly -- don't make things up.\n"
        "4. If the question is about general knowledge (not their specific business), "
        "answer from your training.\n"
        '5. For "what do you know about me?" questions, summarize key facts by category.'
    )


def build_missing_fields_section(contractor: Contractor) -> str:
    """Build a note about missing optional profile fields, if any."""
    missing = get_missing_optional_fields(contractor)
    if not missing:
        return ""
    missing_str = " and ".join(missing)
    return (
        f"Note: You haven't learned this contractor's {missing_str} yet. "
        "If the opportunity comes up naturally in conversation, "
        "try to learn and save these details."
    )


# -----------------------------------------------------------------------
# Pre-built prompt assemblers
# -----------------------------------------------------------------------


async def build_agent_system_prompt(
    db: Session,
    contractor: Contractor,
    tools: list[Tool],
    message_context: str,
) -> str:
    """Assemble the full system prompt for the main agent loop."""
    builder = SystemPromptBuilder()
    builder.set_preamble("You are Backshop, an AI assistant for solo contractors.")

    builder.add_section(
        f"About {contractor.name or 'Contractor'}",
        build_identity_section(contractor),
    )

    memory = await build_memory_section(db, contractor.id, query=message_context)
    builder.add_section("Your Memory", memory)

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

    missing = build_missing_fields_section(contractor)
    if missing:
        builder.add_section("Profile Gaps", missing)

    return builder.build()


async def build_heartbeat_system_prompt(
    db: Session,
    contractor: Contractor,
    flags: list[str],
    recent_messages: str,
) -> str:
    """Assemble the system prompt for the heartbeat evaluator."""
    builder = SystemPromptBuilder()
    builder.set_preamble(
        "You are Backshop's heartbeat evaluator. Your job is to compose a short, "
        "actionable message for the contractor based on the flags below."
    )

    builder.add_section("About the contractor", build_identity_section(contractor))

    memory = await build_memory_section(db, contractor.id)
    builder.add_section("Contractor's memory", memory)

    builder.add_section(
        "Recent conversation (last 5 messages)",
        recent_messages or "(no recent messages)",
    )

    builder.add_section(
        "Flags raised by pre-checks",
        "\n".join(f"- {f}" for f in flags),
    )

    builder.add_section(
        "Current time",
        datetime.datetime.now(datetime.UTC).isoformat(),
    )

    rules = (
        "- The pre-checks already decided something needs attention. Your job is to "
        "compose one concise, helpful message.\n"
        "- Combine multiple flags into a single message when possible.\n"
        "- Keep the message under 160 characters.\n"
        "- Be direct and actionable, no fluff.\n"
        "- If after reviewing the flags you believe none actually warrant a message "
        'right now, you may still choose "no_action".\n'
        "- Use the compose_message tool to return your decision."
    )
    builder.add_section("Rules", rules)

    return builder.build()
