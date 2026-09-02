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

from backend.app.agent.messages import AssistantMessage, UserMessage
from backend.app.agent.session_db import reset_session_stores
from backend.app.config import settings
from backend.app.models import ChatSession, Message, User
from backend.app.services.llm_eval.sampling import (
    ReplayFixture,
    _history_for,
    build_fixture,
    select_samples,
)

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
