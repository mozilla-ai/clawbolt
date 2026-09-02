"""Turn selection and prompt reconstruction for the model-swap evaluator.

Reconstruction runs against *today's* system prompt, tool set, and memory,
not the versions in force when the turn originally happened. That is
deliberate: the decision being made is "should this user move to model X
now", so the prompt the candidate sees should be the prompt it would
actually get. Replaying a months-old system prompt would measure a
configuration nobody is going to ship.

The history for a turn is the window of rows immediately preceding it,
bounded by ``conversation_history_limit`` and then trimmed by the same
``trim_messages`` governor the live loop uses. The session's current
``last_trim_seq`` watermark is deliberately *not* applied: it describes
what is visible today, and applying it would erase the history that older
samples actually ran with.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from backend.app.agent.context import _stored_messages_to_agent_messages
from backend.app.agent.core import AssembledPrompt, ClawboltAgent
from backend.app.agent.dto import StoredMessage
from backend.app.agent.messages import AgentMessage, AssistantMessage
from backend.app.agent.router import init_storage
from backend.app.agent.session_db import get_session_store
from backend.app.agent.tools.base import Tool, tool_to_function_schema
from backend.app.agent.tools.registry import (
    ToolContext,
    default_registry,
    ensure_tool_modules_imported,
)
from backend.app.bus import OutboundMessage
from backend.app.config import settings
from backend.app.enums import MessageDirection
from backend.app.models import User
from backend.app.services.llm_eval.types import ReplaySample

logger = logging.getLogger(__name__)


async def _refuse_outbound(message: OutboundMessage) -> None:
    """Outbound sink for replay tool contexts. Must never be reached."""
    raise AssertionError(
        "llm_eval attempted to publish an outbound message; a replay must never execute a tool"
    )


@dataclass
class ReplayFixture:
    """Everything reusable across every turn in one evaluation run.

    The tool list is built once and shared. Tool *schemas* depend only on
    each tool's params model, never on the turn being replayed, so building
    them per sample would re-run every integration's ``auth_check`` for an
    identical result. The per-turn text that some factories close over
    (``turn_text``) affects tool behavior, never the schema the model sees,
    and no tool is executed during a replay.
    """

    user: User
    rows: list[StoredMessage]
    tools: list[Tool] = field(default_factory=list)
    tool_schemas: list[dict] = field(default_factory=list)
    tools_by_name: dict[str, Tool] = field(default_factory=dict)

    @property
    def tz_name(self) -> str:
        return self.user.timezone or ""


async def build_fixture(user: User) -> ReplayFixture:
    """Load the user's full transcript and materialize their live tool set."""
    store = get_session_store(user.id)
    sessions = await store.list_sessions_async()
    rows: list[StoredMessage] = []
    for session in sessions:
        rows.extend(session.messages)
    rows.sort(key=lambda m: m.seq)

    # Nothing populates the registry at startup: every entry point that needs
    # tools calls this itself (``tool_assembly``, ``heartbeat``, ``onboarding``,
    # ``approval``, ``user_tools``). Without it a run on a worker that has not
    # yet processed a message builds an empty tool list, and then both models
    # are offered no tools, agree perfectly on replying in prose, and the report
    # reads as a clean pass. Silent and completely wrong.
    ensure_tool_modules_imported()

    storage = await init_storage(user)
    context = ToolContext(
        user=user,
        storage=storage,
        # The messaging factory is registered ``requires_outbound=True`` and
        # asserts on this, so passing None drops ``send_media_reply`` from the
        # schema and the replay would offer a smaller tool list than production
        # does. It is never invoked: a replay stops at the model's decision and
        # executes nothing. It raises rather than passing so that if that
        # invariant is ever broken, the run fails loudly instead of publishing
        # a real message to a real user's channel.
        publish_outbound=_refuse_outbound,
        channel=user.preferred_channel or "",
        to_address="",
        downloaded_media=[],
        turn_text="",
    )
    tools = await default_registry.create_core_tools(context)
    tools.extend(await default_registry.create_ready_specialist_tools(context))

    fixture = ReplayFixture(user=user, rows=rows, tools=tools)
    fixture.tool_schemas = [tool_to_function_schema(t) for t in tools]
    fixture.tools_by_name = {t.name: t for t in tools}
    logger.info(
        "Replay fixture for user %s: %d rows, %d tools",
        user.id,
        len(rows),
        len(tools),
    )
    return fixture


def _historic_response(rows: list[StoredMessage], start: int) -> tuple[str, list[str]]:
    """Return the reply text and tool names the agent produced for a turn.

    Walks forward from the inbound row to the next inbound, since one user
    turn can persist several outbound rows. Tool names are read back through
    the same rebuilder the LLM history uses, so a row whose
    ``tool_interactions_json`` is malformed degrades to "no tools" here
    exactly as it does in the prompt.
    """
    reply_parts: list[str] = []
    tool_names: list[str] = []
    for row in rows[start + 1 :]:
        if row.direction == MessageDirection.INBOUND:
            break
        for msg in _stored_messages_to_agent_messages([row]):
            if isinstance(msg, AssistantMessage):
                tool_names.extend(tc.name for tc in msg.tool_calls)
        text = row.llm_reply_text or row.body
        if text:
            reply_parts.append(text)
    return "\n\n".join(reply_parts), tool_names


def select_samples(fixture: ReplayFixture, limit: int) -> list[ReplaySample]:
    """Pick the most recent *limit* inbound turns, oldest first.

    Blank inbound rows are skipped: rapid-fire attachment batching persists
    a placeholder with no body and no processed context, and replaying one
    would ask both models to respond to an empty string.
    """
    samples: list[ReplaySample] = []
    for index, row in enumerate(fixture.rows):
        if row.direction != MessageDirection.INBOUND:
            continue
        message_context = row.processed_context or row.body
        if not message_context.strip():
            continue
        reply, tool_names = _historic_response(fixture.rows, index)
        samples.append(
            ReplaySample(
                seq=row.seq,
                timestamp=row.timestamp,
                message_context=message_context,
                historic_reply=reply,
                historic_tool_names=tool_names,
            )
        )
    return samples[-limit:] if limit > 0 else samples


def _history_for(fixture: ReplayFixture, sample: ReplaySample) -> list[AgentMessage]:
    """Rebuild the conversation history as it stood just before *sample*."""
    preceding = [r for r in fixture.rows if r.seq < sample.seq]
    window = preceding[-settings.conversation_history_limit :]
    return _stored_messages_to_agent_messages(window, tz_name=fixture.tz_name)


async def assemble_for_sample(fixture: ReplayFixture, sample: ReplaySample) -> AssembledPrompt:
    """Build the exact prompt the live agent would send for this turn.

    A fresh ``ClawboltAgent`` per sample keeps turns independent: the agent
    accumulates per-turn state (delivered SKILL.md categories, the last
    reported input-token count) that would otherwise leak from one replayed
    turn into the next and change the prompt. Construction is cheap; the
    expensive part, the tool list, is shared via the fixture.
    """
    agent = ClawboltAgent(user=fixture.user)
    agent.register_tools(fixture.tools)
    return await agent.assemble_prompt(
        sample.message_context,
        _history_for(fixture, sample),
    )
