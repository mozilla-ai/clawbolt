"""Turn selection and history reconstruction for the model-swap evaluator.

The property that matters: replaying turn N must show the model exactly what
the agent saw before turn N, and nothing that came after. A slice that leaks
later turns would let the candidate answer with hindsight and quietly inflate
every agreement number in the report.
"""

from __future__ import annotations

import datetime as _dt
import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.orm import Session

from backend.app.agent.dto import StoredMessage
from backend.app.agent.messages import AssistantMessage, UserMessage
from backend.app.agent.session_db import reset_session_stores
from backend.app.config import settings
from backend.app.models import ChatSession, Message, User
from backend.app.services.llm_eval.sampling import (
    ReplayFixture,
    _historic_response,
    _history_for,
    _sample_clock,
    assemble_for_sample,
    build_fixture,
    select_samples,
)
from backend.app.services.llm_eval.types import ReplaySample

BASE_TIME = _dt.datetime(2026, 5, 1, 12, 0, tzinfo=_dt.UTC)


def _seed(db: Session, user: User, turns: list[tuple[str, str, list[dict] | None]]) -> None:
    """Write a transcript. Each entry is ``(direction, body, tool_interactions)``."""
    session = ChatSession(session_id=str(uuid.uuid4()), user_id=user.id, channel="telegram")
    db.add(session)
    db.commit()
    db.refresh(session)
    for index, (direction, body, tools) in enumerate(turns, start=1):
        db.add(
            Message(
                session_id=session.id,
                seq=index,
                direction=direction,
                body=body,
                processed_context=body if direction == "inbound" else "",
                llm_reply_text=body if direction == "outbound" else "",
                tool_interactions_json=json.dumps(tools) if tools else "",
                timestamp=BASE_TIME + _dt.timedelta(minutes=index),
            )
        )
    db.commit()


@pytest.fixture()
def _reset_stores() -> None:
    reset_session_stores()


async def _fixture_for(user: User) -> ReplayFixture:
    fixture = await build_fixture(user)
    return fixture


@pytest.mark.asyncio()
async def test_selects_only_inbound_turns_most_recent_last(
    db_session: Session, test_user: User, _reset_stores: None
) -> None:
    _seed(
        db_session,
        test_user,
        [
            ("inbound", "first ask", None),
            ("outbound", "first answer", None),
            ("inbound", "second ask", None),
            ("outbound", "second answer", None),
        ],
    )
    fixture = await _fixture_for(test_user)
    samples = select_samples(fixture, limit=10)
    assert [s.message_context for s in samples] == ["first ask", "second ask"]
    assert [s.seq for s in samples] == [1, 3]


@pytest.mark.asyncio()
async def test_limit_keeps_the_most_recent_turns(
    db_session: Session, test_user: User, _reset_stores: None
) -> None:
    _seed(db_session, test_user, [("inbound", f"ask {i}", None) for i in range(1, 6)])
    fixture = await _fixture_for(test_user)
    samples = select_samples(fixture, limit=2)
    assert [s.message_context for s in samples] == ["ask 4", "ask 5"]


@pytest.mark.asyncio()
async def test_blank_inbound_placeholders_are_skipped(
    db_session: Session, test_user: User, _reset_stores: None
) -> None:
    """Attachment batching persists an empty inbound row; replaying it would
    ask both models to respond to nothing."""
    _seed(
        db_session,
        test_user,
        [("inbound", "", None), ("inbound", "real question", None)],
    )
    fixture = await _fixture_for(test_user)
    samples = select_samples(fixture, limit=10)
    assert [s.message_context for s in samples] == ["real question"]


@pytest.mark.asyncio()
async def test_historic_tool_calls_are_recovered(
    db_session: Session, test_user: User, _reset_stores: None
) -> None:
    _seed(
        db_session,
        test_user,
        [
            ("inbound", "book it", None),
            (
                "outbound",
                "Booked.",
                [
                    {
                        "tool_call_id": "t1",
                        "name": "create_job",
                        "args": {"customer": "Acme Plumbing"},
                        "result": "ok",
                    }
                ],
            ),
        ],
    )
    fixture = await _fixture_for(test_user)
    samples = select_samples(fixture, limit=10)
    assert samples[0].historic_tool_names == ["create_job"]
    assert samples[0].historic_reply == "Booked."


@pytest.mark.asyncio()
async def test_history_slice_excludes_the_turn_and_everything_after(
    db_session: Session, test_user: User, _reset_stores: None
) -> None:
    _seed(
        db_session,
        test_user,
        [
            ("inbound", "old ask", None),
            ("outbound", "old answer", None),
            ("inbound", "target ask", None),
            ("outbound", "future answer", None),
            ("inbound", "future ask", None),
        ],
    )
    fixture = await _fixture_for(test_user)
    target = next(s for s in select_samples(fixture, limit=10) if s.message_context == "target ask")

    history = _history_for(fixture, target)
    rendered = [m.content for m in history if isinstance(m, UserMessage | AssistantMessage)]
    joined = " ".join(c or "" for c in rendered)

    assert "old ask" in joined
    assert "old answer" in joined
    # The turn itself is supplied separately as the current message, and
    # anything later had not happened yet.
    assert "target ask" not in joined
    assert "future answer" not in joined
    assert "future ask" not in joined


@pytest.mark.asyncio()
async def test_user_with_no_messages_yields_no_samples(
    test_user: User, _reset_stores: None
) -> None:
    fixture = await _fixture_for(test_user)
    assert select_samples(fixture, limit=100) == []


@pytest.mark.asyncio()
@patch("backend.app.agent.router.oauth_service.load_token", new_callable=AsyncMock)
@patch("backend.app.agent.router.oauth_service.get_valid_token", new_callable=AsyncMock)
async def test_building_a_fixture_never_refreshes_the_users_drive_token(
    mock_get_valid_token: AsyncMock,
    mock_load_token: AsyncMock,
    test_user: User,
    _reset_stores: None,
) -> None:
    """A run must not rotate the grant or tell the user Drive disconnected.

    ``get_valid_token`` writes ``oauth_tokens`` on a refresh and, when the
    refresh fails permanently, deletes the grant and messages the user. The
    fixture only needs to know whether Drive is connected, so it reads the
    stored token instead.
    """
    mock_load_token.return_value = None
    with (
        patch.object(settings, "google_drive_client_id", "client-id"),
        patch.object(settings, "google_drive_client_secret", "client-secret"),
    ):
        await build_fixture(test_user)

    mock_get_valid_token.assert_not_awaited()
    # ``oauth_service`` is a singleton, so every specialist auth_check in the
    # fixture reads through the same patched ``load_token``. Assert on the Drive
    # read specifically rather than on the call count.
    assert (test_user.id, "google_drive") in {call.args for call in mock_load_token.await_args_list}


# ---------------------------------------------------------------------------
# Bounding the transcript read
#
# Every row is envelope-encrypted, so decryption happens on attribute access
# in this process. A production user with 1827 messages had their whole
# transcript loaded and decrypted to use the last few turns, five times in
# half an hour, on the event loop that also serves their own messages.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_a_bounded_read_gives_the_same_samples_as_a_full_one(
    db_session: Session, test_user: User, _reset_stores: None
) -> None:
    """The bound is an optimization, so it has to be invisible in the result."""
    turns: list[tuple[str, str, list[dict] | None]] = []
    for i in range(1, 61):
        turns.append(("inbound", f"ask {i}", None))
        turns.append(("outbound", f"answer {i}", None))
    _seed(db_session, test_user, turns)

    # The budget is dominated by ``conversation_history_limit`` (500 by
    # default), so it is lowered here rather than seeding a transcript long
    # enough to exceed it.
    # The window has to stay patched through the assertions too: the budget is
    # computed from ``conversation_history_limit`` and ``_history_for`` reads it
    # again at call time, so comparing the two fixtures under a different limit
    # compares windows neither was built for.
    with patch.object(settings, "conversation_history_limit", 20):
        full = await build_fixture(test_user)
        bounded = await build_fixture(test_user, sample_limit=5)

        assert len(bounded.rows) < len(full.rows)
        full_samples = select_samples(full, limit=5)
        bounded_samples = select_samples(bounded, limit=5)
        assert [s.seq for s in bounded_samples] == [s.seq for s in full_samples]
        assert [s.message_context for s in bounded_samples] == [
            s.message_context for s in full_samples
        ]
        assert [s.historic_reply for s in bounded_samples] == [
            s.historic_reply for s in full_samples
        ]
        # And the history each sample reconstructs is the same window.
        assert [m.content for m in _history_for(bounded, bounded_samples[0])] == [
            m.content for m in _history_for(full, full_samples[0])
        ]


@pytest.mark.asyncio()
async def test_the_bound_falls_back_rather_than_shrinking_a_run(
    db_session: Session, test_user: User, _reset_stores: None
) -> None:
    """A window that fills before holding the turns asked for is abandoned.

    One inbound row can be followed by many outbound rows, so a fixed
    allowance per turn is a guess. Getting it wrong must cost a slower load,
    never a smaller evaluation than the operator chose.
    """
    turns: list[tuple[str, str, list[dict] | None]] = []
    for i in range(1, 6):
        turns.append(("inbound", f"ask {i}", None))
        # Twenty outbound rows per turn blows through the per-turn allowance.
        for j in range(20):
            turns.append(("outbound", f"step {i}.{j}", None))
    _seed(db_session, test_user, turns)

    with patch.object(settings, "conversation_history_limit", 20):
        bounded = await build_fixture(test_user, sample_limit=5)
    assert len(select_samples(bounded, limit=5)) == 5


# ---------------------------------------------------------------------------
# Rapid-fire messages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_rapid_fire_turns_all_see_the_response_to_the_batch(
    db_session: Session, test_user: User, _reset_stores: None
) -> None:
    """Four messages in a row are one turn as far as the agent is concerned.

    The agent answers the batch once, after the last message. Reading only up
    to the *next* inbound row reported "the agent did nothing" for the first
    three, and ``check_safety`` then charged the candidate with an
    unrequested mutation on a turn whose text was an explicit instruction to
    write.
    """
    _seed(
        db_session,
        test_user,
        [
            ("inbound", "rebuild the stalls", None),
            ("inbound", "add 5000 for the staircase", None),
            ("inbound", "build and send", None),
            (
                "outbound",
                "sent",
                [
                    {
                        "tool_call_id": "t1",
                        "name": "qb_update",
                        "args": {"estimate_id": "635"},
                        "result": "ok",
                    }
                ],
            ),
        ],
    )
    fixture = await _fixture_for(test_user)
    samples = select_samples(fixture, limit=10)

    assert [s.seq for s in samples] == [1, 2, 3]
    for sample in samples:
        assert sample.historic_tool_names == ["qb_update"], (
            f"seq {sample.seq} lost the batch's response"
        )
        assert sample.historic_reply == "sent"


@pytest.mark.asyncio()
async def test_a_trailing_turn_with_no_response_yet_reports_no_tools(
    db_session: Session, test_user: User, _reset_stores: None
) -> None:
    """The honest answer for an unanswered turn is "nothing", not a guess."""
    _seed(
        db_session,
        test_user,
        [
            ("inbound", "first", None),
            ("outbound", "answered", None),
            ("inbound", "still waiting", None),
        ],
    )
    fixture = await _fixture_for(test_user)
    samples = select_samples(fixture, limit=10)

    trailing = next(s for s in samples if s.seq == 3)
    assert trailing.historic_tool_names == []
    assert trailing.historic_reply == ""


# ---------------------------------------------------------------------------
# The replay clock is the turn's own timestamp
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_replayed_turn_is_stamped_with_its_own_time_not_now(
    db_session: Session, test_user: User, _reset_stores: None
) -> None:
    """A date-relative ask must resolve against the day it was sent.

    History rows render absolute date markers, so a run days later hands the
    model a conversation that ends last week under a header claiming today.
    Both models then resolve "this past week" to the wrong week, and the
    calendar arguments they are scored on are wrong for a reason that has
    nothing to do with either model.
    """
    _seed(
        db_session,
        test_user,
        [("inbound", "put him down for Monday to Thursday this past week", None)],
    )
    fixture = await _fixture_for(test_user)
    sample = select_samples(fixture, limit=1)[0]

    assembled = await assemble_for_sample(fixture, sample)
    turn_text = assembled.messages[-1].content
    assert isinstance(turn_text, str)

    # The stamp is the turn's own time, to the minute the row carries.
    assert "[Current time: Friday, 2026-05-01 12:01 PM" in turn_text, turn_text
    assert _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%d") not in turn_text


def test_sample_clock_parses_the_stored_timestamp() -> None:
    sample = ReplaySample(
        seq=1, timestamp="2026-08-30T16:48:00+00:00", message_context="this past week"
    )
    assert _sample_clock(sample) == _dt.datetime(2026, 8, 30, 16, 48, tzinfo=_dt.UTC)


def test_sample_clock_falls_back_to_wall_time_on_a_corrupt_timestamp() -> None:
    """A wrong clock is worse than the honest current one."""
    sample = ReplaySample(seq=1, timestamp="not a timestamp", message_context="hi")
    assert _sample_clock(sample) is None


def test_sample_clock_assumes_utc_for_a_naive_timestamp() -> None:
    sample = ReplaySample(seq=1, timestamp="2026-08-30T16:48:00", message_context="hi")
    clock = _sample_clock(sample)
    assert clock is not None
    assert clock.tzinfo is not None


# ---------------------------------------------------------------------------
# A batch is bounded in time; a long gap means the turn went unanswered
# ---------------------------------------------------------------------------


def _row(seq: int, direction: str, body: str, at: _dt.datetime, tools: str = "") -> StoredMessage:
    return StoredMessage(
        seq=seq,
        direction=direction,
        body=body,
        timestamp=at.isoformat(),
        llm_reply_text=body if direction == "outbound" else "",
        tool_interactions_json=tools,
    )


_WRITE = '[{"tool_call_id": "t1", "name": "qb_send", "args": {}, "result": "ok"}]'


def test_rapid_fire_rows_seconds_apart_share_the_batch_response() -> None:
    rows = [
        _row(1, "inbound", "rebuild the stalls", BASE_TIME),
        _row(2, "inbound", "build and send", BASE_TIME + _dt.timedelta(seconds=2)),
        _row(3, "outbound", "sent", BASE_TIME + _dt.timedelta(seconds=8), tools=_WRITE),
    ]
    assert _historic_response(rows, 0) == ("sent", ["qb_send"])


def test_an_orphaned_turn_is_not_credited_with_a_later_turns_tool_calls() -> None:
    """An inbound the agent never answered did nothing, whatever came next.

    ``agent.inbound_recovery`` records a production inbound that sat 29 hours
    before the next message woke a batcher. Reading the whole run of inbound
    rows as one batch credits that turn with the later turn's writes, and
    ``check_safety`` then treats a candidate that invoices a customer in reply
    to "just checking in" as doing what the live agent did.
    """
    rows = [
        _row(1, "inbound", "just checking in", BASE_TIME),
        _row(2, "inbound", "go ahead and send it", BASE_TIME + _dt.timedelta(hours=3)),
        _row(3, "outbound", "sent", BASE_TIME + _dt.timedelta(hours=3, seconds=6), tools=_WRITE),
    ]
    assert _historic_response(rows, 0) == ("", [])
    assert _historic_response(rows, 1) == ("sent", ["qb_send"])


def test_a_corrupt_timestamp_does_not_hand_out_a_batch_exemption() -> None:
    rows = [
        _row(1, "inbound", "first", BASE_TIME),
        _row(2, "inbound", "second", BASE_TIME),
        _row(3, "outbound", "sent", BASE_TIME, tools=_WRITE),
    ]
    rows[1].timestamp = "not a timestamp"
    assert _historic_response(rows, 0) == ("", [])
