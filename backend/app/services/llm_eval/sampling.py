"""Turn selection and prompt reconstruction for the model-swap evaluator.

Reconstruction runs against *today's* system prompt, tool set, and memory,
not the versions in force when the turn originally happened. That is
deliberate: the decision being made is "should this user move to model X
now", so the prompt the candidate sees should be the prompt it would
actually get. Replaying a months-old system prompt would measure a
configuration nobody is going to ship.

The clock is the one exception, and it is not a configuration choice. The
turn's own timestamp is stamped on the replayed turn, because the history
rows around it render absolute date markers: a run days later would ask the
model to read a conversation that ended last week under a header claiming
today, and every date-relative instruction in it would resolve to the wrong
day. See ``assemble_for_sample``.

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
from datetime import UTC, datetime

from backend.app.agent.approval import get_approval_store
from backend.app.agent.context import _stored_messages_to_agent_messages
from backend.app.agent.core import AssembledPrompt, ClawboltAgent
from backend.app.agent.dto import StoredMessage
from backend.app.agent.messages import AgentMessage, AssistantMessage
from backend.app.agent.router import init_storage
from backend.app.agent.session_db import get_session_store
from backend.app.agent.stores import ToolConfigStore
from backend.app.agent.tools.base import Tool, tool_to_function_schema
from backend.app.agent.tools.registry import (
    ToolContext,
    create_list_capabilities_tool,
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


# Rows a single user turn can occupy. One inbound row plus the outbound rows
# the agent wrote answering it; four covers a turn with several tool receipts
# and a final reply, and the fallback below covers the rest.
_ROWS_PER_TURN_ALLOWANCE = 6


def _row_budget(sample_limit: int) -> int:
    """Rows worth loading to sample ``sample_limit`` turns with their history.

    A run needs the last N inbound turns, the outbound rows that follow each of
    them, and ``conversation_history_limit`` rows before the oldest one, since
    that is the window ``_history_for`` slices. Everything older is loaded,
    decrypted, and discarded.
    """
    return sample_limit * _ROWS_PER_TURN_ALLOWANCE + settings.conversation_history_limit


async def build_fixture(user: User, *, sample_limit: int | None = None) -> ReplayFixture:
    """Materialize the user's live tool set and enough transcript to replay.

    ``sample_limit`` bounds the transcript read to the tail a run of that size
    can reach. Every row is envelope-encrypted, so decryption happens on
    attribute access in this process: loading a long transcript to use its last
    few turns spends real CPU on the event loop that also serves the user's own
    messages. Omit it to load everything, which is what a caller that does not
    know its sample count has to do.
    """
    store = get_session_store(user.id)
    rows: list[StoredMessage] = []
    if sample_limit is not None and sample_limit > 0:
        budget = _row_budget(sample_limit)
        rows = await store.get_recent_messages_async(budget)
        inbound = sum(1 for r in rows if r.direction == MessageDirection.INBOUND)
        if len(rows) == budget and inbound < sample_limit:
            # The window filled up before it held the turns asked for, which
            # means this user's turns are unusually long. Fall back rather than
            # quietly running a smaller evaluation than the operator chose.
            logger.info(
                "Replay window of %d rows held only %d inbound turn(s) for user %s; "
                "loading the full transcript",
                budget,
                inbound,
                user.id,
            )
            rows = []
    if not rows:
        sessions = await store.list_sessions_async()
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

    # ``refresh=False``: a replay must not write the user's state or message
    # them. Refreshing an expired Drive token writes ``oauth_tokens``, and a
    # permanent refresh failure deletes the grant and sends the user a
    # "Drive disconnected" message, which the ``_refuse_outbound`` sink below
    # cannot intercept because it goes straight to the message bus. The token
    # here is only ever read for its presence, to keep the file tools on the
    # schema, so an expired one is as good as a fresh one.
    storage = await init_storage(user, refresh=False)
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
    # Mirror ``router.run_agent_step`` exactly. A tool group the user disabled,
    # or a sub-tool they set to NEVER, is absent from the schema the live agent
    # is offered *and* from the tool-guidelines section of the system prompt, so
    # including it here would score a prompt this user never received and let a
    # candidate "call" a tool production would not have exposed.
    #
    # ``approval_store.ensure_complete`` is deliberately not called: it writes
    # PERMISSIONS.json, and a replay must not mutate the user's state. It only
    # ever backfills tools at their *default* level, and no default is NEVER,
    # so skipping it cannot change the set read back here.
    disabled_groups = await ToolConfigStore(user.id).get_disabled_tool_names()
    disabled_sub_tools = await get_approval_store().get_never_tool_names(user.id)

    tools = await default_registry.create_core_tools(
        context,
        excluded_factories=disabled_groups or None,
        excluded_tool_names=disabled_sub_tools or None,
    )
    tools.extend(
        await default_registry.create_ready_specialist_tools(
            context,
            excluded_factories=disabled_groups or None,
            excluded_tool_names=disabled_sub_tools or None,
        )
    )
    # ``list_capabilities`` is on almost every real turn (any unconnected
    # integration is enough to add it) and carries a usage hint of its own, so
    # omitting it changed both the schema and the system prompt.
    specialist_summaries = await default_registry.get_available_specialist_summaries(
        context, excluded_factories=disabled_groups or None
    )
    unauthenticated = await default_registry.get_unauthenticated_specialists(
        context, excluded_factories=disabled_groups or None
    )
    if specialist_summaries or unauthenticated:
        disabled_specialist_subs = default_registry.get_disabled_specialist_sub_tools(
            disabled_sub_tools or set()
        )
        tools.append(
            create_list_capabilities_tool(
                specialist_summaries,
                unauthenticated=unauthenticated,
                disabled_sub_tools=disabled_specialist_subs or None,
            )
        )

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

     A "turn" here is the whole run of consecutive inbound rows starting at
     *start*, plus the outbound rows that follow it. Skipping over the rest of
     the inbound run is what makes this correct for rapid-fire messages: a user
     who sends four messages in a row persists four inbound rows and the agent
     answers the batch once, after the last of them. Reading only up to the
     *next* inbound row reported "the agent did nothing" for the first three,
     and ``check_safety`` then charged the candidate with an unrequested
     mutation on a turn whose text was an explicit instruction to write
    .

     Tool names are read back through the same rebuilder the LLM history uses,
     so a row whose ``tool_interactions_json`` is malformed degrades to "no
     tools" here exactly as it does in the prompt.
    """
    reply_parts: list[str] = []
    tool_names: list[str] = []
    index = start + 1
    # Advance past the remainder of the inbound batch this row belongs to.
    while index < len(rows) and rows[index].direction == MessageDirection.INBOUND:
        index += 1
    for row in rows[index:]:
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


def _sample_clock(sample: ReplaySample) -> datetime | None:
    """The wall time to stamp on *sample*'s replayed turn, or None for now.

    Falls back to None (wall time) on an unparseable timestamp rather than
    guessing: a wrong clock is worse than the honest current one, and the
    column is ISO-formatted by ``_turn_row`` so this is a corrupt-row guard,
    not an expected branch.
    """
    try:
        parsed = datetime.fromisoformat(sample.timestamp)
    except (TypeError, ValueError):
        logger.warning("Unparseable timestamp %r on seq %d", sample.timestamp, sample.seq)
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


async def assemble_for_sample(fixture: ReplayFixture, sample: ReplaySample) -> AssembledPrompt:
    """Build the exact prompt the live agent would send for this turn.

    A fresh ``ClawboltAgent`` per sample keeps turns independent: the agent
    accumulates per-turn state (delivered SKILL.md categories, the last
    reported input-token count) that would otherwise leak from one replayed
    turn into the next and change the prompt. Construction is cheap; the
    expensive part, the tool list, is shared via the fixture.

    The clock is the turn's own timestamp, not now. Everything else about the
    prompt is deliberately today's (see the module docstring), but the clock
    is not a configuration choice: history rows render absolute date markers,
    so a replay run days later hands the model a conversation that ends last
    week under a header saying today, and "book it for this past Thursday"
    lands on the wrong Thursday for both models.
    """
    agent = ClawboltAgent(user=fixture.user)
    agent.register_tools(fixture.tools)
    # Reproducibility: without this the trim decision reads a process-local
    # estimate written by live traffic, so the same run can build a different
    # prompt on a worker that recently served this user.
    return await agent.assemble_prompt(
        sample.message_context,
        _history_for(fixture, sample),
        deterministic_trim=True,
        now=_sample_clock(sample),
    )
