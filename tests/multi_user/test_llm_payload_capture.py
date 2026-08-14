"""Tests for the premium LLM-payload capture observer.

Covers:
- Consent gating (no row written for users without ``data_sharing_consent``).
- Single-era capture (overwrites current within the same era).
- Compaction-driven rotation (era marker change rotates current → previous).
- Concurrent captures for the same user (rotation is consistent).
- Oversize-payload drop.
- Lazy purge when consent flips off between captures.
- Response-side capture (paired by request_id, drops on no match).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.agent.observer import (
    PURPOSE_AGENT_MAIN,
    PURPOSE_COMPACTION,
    LLMRequestPayload,
    LLMResponsePayload,
)
from backend.app.config import Settings
from backend.app.database import db_session_async
from backend.app.models import LLMPayloadCapture, User
from backend.app.services.llm_payload_capture import (
    MAX_CAPTURE_BYTES,
    _payload_to_json_dict,
    capture_llm_request,
    capture_llm_response,
)


def _make_payload(
    *,
    user_id: str,
    min_seq: int | None,
    request_id: str = "req-x",
    model: str = "claude-test",
    purpose: str = PURPOSE_AGENT_MAIN,
    extra_messages: list[dict[str, Any]] | None = None,
) -> LLMRequestPayload:
    return LLMRequestPayload(
        schema_version=1,
        purpose=purpose,
        user_id=user_id,
        session_id="sess-1",
        request_id=request_id,
        model=model,
        provider="anthropic",
        max_tokens=1024,
        thinking=None,
        system="you are helpful",
        messages=extra_messages or [{"role": "user", "content": "hello"}],
        tools=[{"name": "echo", "description": "echo", "input_schema": {}}],
        min_message_seq_in_prompt=min_seq,
        started_at=datetime.now(UTC),
    )


async def _insert_user(
    async_db: async_sessionmaker,
    *,
    consent: bool,
) -> str:
    new_id = str(uuid.uuid4())
    async with async_db() as db:
        user = User(
            id=new_id,
            user_id=f"capture-test-{new_id[:8]}",
            phone="",
            channel_identifier=f"ch-{new_id[:8]}",
            preferred_channel="telegram",
            onboarding_complete=True,
            data_sharing_consent=consent,
        )
        db.add(user)
        await db.commit()
    return new_id


async def _read_capture(async_db: async_sessionmaker, user_id: str) -> LLMPayloadCapture | None:
    async with async_db() as db:
        return (
            await db.execute(select(LLMPayloadCapture).where(LLMPayloadCapture.user_id == user_id))
        ).scalar_one_or_none()


@pytest.mark.asyncio()
async def test_capture_skipped_when_no_consent(async_db: async_sessionmaker) -> None:
    user_id = await _insert_user(async_db, consent=False)
    await capture_llm_request(_make_payload(user_id=user_id, min_seq=10))

    row = await _read_capture(async_db, user_id)
    assert row is None


@pytest.mark.asyncio()
async def test_capture_skips_non_agent_main_purposes(
    async_db: async_sessionmaker,
) -> None:
    """Compaction / heartbeat / agent-followup payloads carry no era marker
    that fits the rotation logic; capturing them would ping-pong the slot.
    Filter at the entrypoint so the rotation only tracks agent-main calls."""
    user_id = await _insert_user(async_db, consent=True)
    await capture_llm_request(
        _make_payload(user_id=user_id, min_seq=None, purpose=PURPOSE_COMPACTION)
    )

    row = await _read_capture(async_db, user_id)
    assert row is None


@pytest.mark.asyncio()
async def test_capture_writes_current_era_for_consenting_user(
    async_db: async_sessionmaker,
) -> None:
    user_id = await _insert_user(async_db, consent=True)
    payload = _make_payload(user_id=user_id, min_seq=42, request_id="req-A")

    await capture_llm_request(payload)

    row = await _read_capture(async_db, user_id)
    assert row is not None
    assert row.current_era_min_message_seq == 42
    assert row.current_era_request_id == "req-A"
    assert row.current_era_payload_bytes > 0
    assert row.current_era_payload["model"] == "claude-test"
    assert row.previous_era_payload is None
    assert row.previous_era_min_message_seq is None


@pytest.mark.asyncio()
async def test_same_era_marker_overwrites_current_only(
    async_db: async_sessionmaker,
) -> None:
    user_id = await _insert_user(async_db, consent=True)

    # Three captures in the same era (same min_seq) should leave previous
    # NULL and current overwritten with the latest payload.
    await capture_llm_request(_make_payload(user_id=user_id, min_seq=5, request_id="r1"))
    await capture_llm_request(_make_payload(user_id=user_id, min_seq=5, request_id="r2"))
    await capture_llm_request(_make_payload(user_id=user_id, min_seq=5, request_id="r3"))

    row = await _read_capture(async_db, user_id)
    assert row is not None
    assert row.current_era_min_message_seq == 5
    assert row.current_era_request_id == "r3"
    assert row.previous_era_payload is None


@pytest.mark.asyncio()
async def test_era_change_rotates_current_into_previous(
    async_db: async_sessionmaker,
) -> None:
    user_id = await _insert_user(async_db, consent=True)

    # Era A: msg seq 1 (last payload from era A is request_id="A2"). Distinct
    # message bodies so the rotation can be verified content-wise -- a SQL
    # refactor that swapped ``c.X`` and ``excluded.X`` would silently invert
    # the rotation if we only asserted request_id.
    await capture_llm_request(
        _make_payload(
            user_id=user_id,
            min_seq=1,
            request_id="A1",
            extra_messages=[{"role": "user", "content": "era-A-msg-1"}],
        )
    )
    await capture_llm_request(
        _make_payload(
            user_id=user_id,
            min_seq=1,
            request_id="A2",
            extra_messages=[{"role": "user", "content": "era-A-msg-2"}],
        )
    )

    # Compaction lifts the floor: era B starts at min_seq=4.
    await capture_llm_request(
        _make_payload(
            user_id=user_id,
            min_seq=4,
            request_id="B1",
            extra_messages=[{"role": "user", "content": "era-B-msg-1"}],
        )
    )

    row = await _read_capture(async_db, user_id)
    assert row is not None
    # Previous era holds the *last* payload from era A (both request_id and
    # the actual messages content; this guards the c-vs-excluded SQL swap).
    assert row.previous_era_min_message_seq == 1
    assert row.previous_era_request_id == "A2"
    assert row.previous_era_payload is not None
    assert row.previous_era_payload["messages"] == [{"role": "user", "content": "era-A-msg-2"}]
    # Current era is the new B payload.
    assert row.current_era_min_message_seq == 4
    assert row.current_era_request_id == "B1"
    assert row.current_era_payload["messages"] == [{"role": "user", "content": "era-B-msg-1"}]

    # A second compaction (era C) should drop era A entirely.
    await capture_llm_request(
        _make_payload(
            user_id=user_id,
            min_seq=9,
            request_id="C1",
            extra_messages=[{"role": "user", "content": "era-C-msg-1"}],
        )
    )

    row = await _read_capture(async_db, user_id)
    assert row is not None
    assert row.previous_era_min_message_seq == 4
    assert row.previous_era_request_id == "B1"
    assert row.previous_era_payload is not None
    assert row.previous_era_payload["messages"] == [{"role": "user", "content": "era-B-msg-1"}]
    assert row.current_era_min_message_seq == 9
    assert row.current_era_request_id == "C1"
    assert row.current_era_payload["messages"] == [{"role": "user", "content": "era-C-msg-1"}]


@pytest.mark.asyncio()
async def test_null_to_int_era_marker_rotates(
    async_db: async_sessionmaker,
) -> None:
    """A fresh-session capture (NULL min_seq) followed by a real-seq
    capture must rotate, since ``IS DISTINCT FROM`` treats NULL as
    different from any integer."""
    user_id = await _insert_user(async_db, consent=True)
    await capture_llm_request(_make_payload(user_id=user_id, min_seq=None, request_id="N1"))
    await capture_llm_request(_make_payload(user_id=user_id, min_seq=7, request_id="S1"))

    row = await _read_capture(async_db, user_id)
    assert row is not None
    assert row.previous_era_min_message_seq is None
    assert row.previous_era_request_id == "N1"
    assert row.current_era_min_message_seq == 7
    assert row.current_era_request_id == "S1"


@pytest.mark.asyncio()
async def test_oversize_payload_is_dropped(async_db: async_sessionmaker) -> None:
    user_id = await _insert_user(async_db, consent=True)

    # Build a payload that exceeds the cap by stuffing the messages list.
    huge_chunk = "x" * (MAX_CAPTURE_BYTES // 4)
    msgs = [{"role": "user", "content": huge_chunk} for _ in range(8)]
    payload = _make_payload(user_id=user_id, min_seq=1, extra_messages=msgs)

    await capture_llm_request(payload)

    row = await _read_capture(async_db, user_id)
    assert row is None


@pytest.mark.asyncio()
async def test_lazy_purge_when_consent_flips_off(
    async_db: async_sessionmaker,
) -> None:
    """If a user revokes consent between captures, the next observer
    fire (or any subsequent capture attempt) must remove the lingering
    row even though no separate consent-revoke hook exists in OSS yet."""
    user_id = await _insert_user(async_db, consent=True)
    await capture_llm_request(_make_payload(user_id=user_id, min_seq=1, request_id="r1"))
    assert await _read_capture(async_db, user_id) is not None

    # Flip consent off.
    async with async_db() as db:
        user = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
        user.data_sharing_consent = False
        await db.commit()

    # Next capture: observer detects no-consent and purges the row.
    await capture_llm_request(_make_payload(user_id=user_id, min_seq=1, request_id="r2"))
    assert await _read_capture(async_db, user_id) is None


# Note: concurrency safety lives at the DB layer. The capture upsert is a
# single ``INSERT ... ON CONFLICT DO UPDATE`` statement, which Postgres
# serializes via the row-level lock acquired by the conflict resolution.
# Two concurrent capture calls for the same user therefore cannot corrupt
# the rotation: the second one sees the first's committed state when it
# acquires the lock. We don't assert this in a unit test because the
# SAVEPOINT-based test fixture does not support two concurrent
# transactions on the same connection -- the property under test is a
# Postgres guarantee, not application logic.


def test_chat_sessions_user_id_is_unique() -> None:
    """Pin the single-session-per-user invariant that ``llm_payload_captures``
    relies on. If OSS ever reintroduces multi-session (drops the
    ``UNIQUE(user_id)`` constraint added in migration 026), this test fails
    loudly so the capture-service PK can be migrated to
    ``(user_id, session_id)`` before the rotation logic ping-pongs.

    This is a static check against the ORM mapping; no DB connection
    needed. If the SQLAlchemy model ever drifts from the migration the
    OSS conftest will catch the divergence on bootstrap."""
    from sqlalchemy import Table, UniqueConstraint

    from backend.app.models import ChatSession

    table: Table = ChatSession.__table__  # type: ignore[assignment]
    user_id_unique_constraints = [
        c
        for c in table.constraints
        if isinstance(c, UniqueConstraint) and [col.name for col in c.columns] == ["user_id"]
    ]
    assert user_id_unique_constraints, (
        "chat_sessions.user_id must be UNIQUE (OSS migration 026). "
        "If multi-session was reintroduced, llm_payload_captures.user_id "
        "PK must be migrated to (user_id, session_id) to avoid rotation "
        "ping-pong."
    )


# ---------------------------------------------------------------------------
# Response capture
# ---------------------------------------------------------------------------


def _make_response_payload(
    *,
    user_id: str,
    request_id: str = "req-x",
    purpose: str = PURPOSE_AGENT_MAIN,
    content_blocks: list[dict[str, Any]] | None = None,
) -> LLMResponsePayload:
    started = datetime.now(UTC)
    return LLMResponsePayload(
        schema_version=1,
        purpose=purpose,
        user_id=user_id,
        session_id="sess-1",
        request_id=request_id,
        model="claude-test",
        provider="anthropic",
        content_blocks=content_blocks or [{"type": "text", "text": "hello back"}],
        stop_reason="end_turn",
        input_tokens=10,
        output_tokens=3,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
        started_at=started,
        completed_at=started,
    )


@pytest.mark.asyncio()
async def test_response_capture_writes_to_current_era_when_request_matches(
    async_db: async_sessionmaker,
) -> None:
    """Common case: request captured, then response arrives with the same
    request_id. Response columns land in the current era alongside the
    matching request."""
    user_id = await _insert_user(async_db, consent=True)
    await capture_llm_request(_make_payload(user_id=user_id, min_seq=10, request_id="req-A"))
    await capture_llm_response(_make_response_payload(user_id=user_id, request_id="req-A"))

    row = await _read_capture(async_db, user_id)
    assert row is not None
    assert row.current_era_response is not None
    assert row.current_era_response["content_blocks"][0]["text"] == "hello back"
    assert row.current_era_response["stop_reason"] == "end_turn"
    assert row.current_era_response_captured_at is not None
    assert row.current_era_response_bytes is not None
    assert row.current_era_response_bytes > 0
    # Previous era response stays NULL until the era rotates.
    assert row.previous_era_response is None


@pytest.mark.asyncio()
async def test_response_capture_writes_to_previous_era_after_rotation(
    async_db: async_sessionmaker,
) -> None:
    """If the request was already rotated into the previous slot before
    the matching response arrives, the response lands in the previous
    slot too -- not the current one (whose request_id is different)."""
    user_id = await _insert_user(async_db, consent=True)
    # First request lands in current era.
    await capture_llm_request(_make_payload(user_id=user_id, min_seq=10, request_id="req-A"))
    # Era marker changes -> rotation pushes req-A into previous era.
    await capture_llm_request(_make_payload(user_id=user_id, min_seq=20, request_id="req-B"))
    # Response for req-A arrives late, after rotation.
    await capture_llm_response(_make_response_payload(user_id=user_id, request_id="req-A"))

    row = await _read_capture(async_db, user_id)
    assert row is not None
    assert row.previous_era_request_id == "req-A"
    assert row.previous_era_response is not None
    assert row.previous_era_response["content_blocks"][0]["text"] == "hello back"
    # Current-era response stays unset (its request_id is "req-B").
    assert row.current_era_response is None


@pytest.mark.asyncio()
async def test_response_capture_drops_when_no_matching_request(
    async_db: async_sessionmaker,
) -> None:
    """A response for a request that was never captured (purpose filter,
    consent miss, oversized) must not silently overwrite the existing
    row. Inserting a NULL-request row would lose pairing info; dropping
    is the safer choice."""
    user_id = await _insert_user(async_db, consent=True)
    await capture_llm_request(_make_payload(user_id=user_id, min_seq=10, request_id="req-A"))
    # Response for an unrelated request_id.
    await capture_llm_response(_make_response_payload(user_id=user_id, request_id="req-unknown"))

    row = await _read_capture(async_db, user_id)
    assert row is not None
    assert row.current_era_response is None
    assert row.previous_era_response is None


@pytest.mark.asyncio()
async def test_response_capture_drops_when_no_request_row(
    async_db: async_sessionmaker,
) -> None:
    """If the request half was filtered out (e.g. no consent at the time),
    there is no row to attach to. The response must be dropped, not
    create a partial row."""
    user_id = await _insert_user(async_db, consent=False)
    await capture_llm_response(_make_response_payload(user_id=user_id, request_id="req-A"))

    row = await _read_capture(async_db, user_id)
    assert row is None


@pytest.mark.asyncio()
async def test_response_capture_skips_non_agent_main_purposes(
    async_db: async_sessionmaker,
) -> None:
    """Mirrors the request-side filter: compaction / heartbeat / followup
    responses must not flow into the rotation."""
    user_id = await _insert_user(async_db, consent=True)
    await capture_llm_request(_make_payload(user_id=user_id, min_seq=10, request_id="req-A"))
    await capture_llm_response(
        _make_response_payload(user_id=user_id, request_id="req-A", purpose=PURPOSE_COMPACTION)
    )

    row = await _read_capture(async_db, user_id)
    assert row is not None
    assert row.current_era_response is None


@pytest.mark.asyncio()
async def test_response_capture_skips_when_request_id_is_none(
    async_db: async_sessionmaker,
) -> None:
    """Without a request_id there is no way to pair with the request row;
    the response is dropped rather than guessed."""
    user_id = await _insert_user(async_db, consent=True)
    await capture_llm_request(_make_payload(user_id=user_id, min_seq=10, request_id="req-A"))

    payload = _make_response_payload(user_id=user_id, request_id="req-A")
    # Construct a None-request variant via direct field replacement
    # because the dataclass is frozen.
    payload_no_rid = LLMResponsePayload(
        schema_version=payload.schema_version,
        purpose=payload.purpose,
        user_id=payload.user_id,
        session_id=payload.session_id,
        request_id=None,
        model=payload.model,
        provider=payload.provider,
        content_blocks=payload.content_blocks,
        stop_reason=payload.stop_reason,
        input_tokens=payload.input_tokens,
        output_tokens=payload.output_tokens,
        cache_creation_input_tokens=payload.cache_creation_input_tokens,
        cache_read_input_tokens=payload.cache_read_input_tokens,
        started_at=payload.started_at,
        completed_at=payload.completed_at,
    )
    await capture_llm_response(payload_no_rid)

    row = await _read_capture(async_db, user_id)
    assert row is not None
    assert row.current_era_response is None


@pytest.mark.asyncio()
async def test_response_capture_drops_oversize(
    async_db: async_sessionmaker,
) -> None:
    """A response larger than the cap is dropped (same defensive cap as
    the request side). The matching request stays untouched."""
    user_id = await _insert_user(async_db, consent=True)
    await capture_llm_request(_make_payload(user_id=user_id, min_seq=10, request_id="req-A"))
    bloated_blocks = [{"type": "text", "text": "x" * (MAX_CAPTURE_BYTES + 1024)}]
    await capture_llm_response(
        _make_response_payload(
            user_id=user_id,
            request_id="req-A",
            content_blocks=bloated_blocks,
        )
    )

    row = await _read_capture(async_db, user_id)
    assert row is not None
    assert row.current_era_response is None
    # Request row is unaffected.
    assert row.current_era_payload is not None


@pytest.mark.asyncio()
async def test_response_capture_succeeds_after_request_commits_late() -> None:
    """If the response observer fires before the request has committed,
    the response capture's bounded retry should wait briefly and then
    pair successfully on a subsequent attempt.

    Simulates the production race where both observers spawn separate
    background tasks: the response task can scheduler-win over the
    request task on a fast LLM round.
    """
    # ``async_db`` binds all sessions to one connection under nested
    # SAVEPOINTs. That cannot represent independently committing tasks: their
    # SAVEPOINT release order can be interleaved. The normal test fixture still
    # provides an isolated database and lets this race use separate sessions,
    # as it does in production.
    user_id = str(uuid.uuid4())
    async with db_session_async() as db:
        db.add(
            User(
                id=user_id,
                user_id=f"capture-test-{user_id[:8]}",
                phone="",
                channel_identifier=f"ch-{user_id[:8]}",
                preferred_channel="telegram",
                onboarding_complete=True,
                data_sharing_consent=True,
            )
        )
        await db.commit()

    # Schedule the response capture FIRST, then the request capture after
    # a short delay. The pairing retry should bridge the gap.
    async def delayed_request() -> None:
        await asyncio.sleep(0.04)
        await capture_llm_request(_make_payload(user_id=user_id, min_seq=1, request_id="req-late"))

    async def early_response() -> None:
        await capture_llm_response(_make_response_payload(user_id=user_id, request_id="req-late"))

    await asyncio.gather(early_response(), delayed_request())

    async with db_session_async() as db:
        row = (
            await db.execute(select(LLMPayloadCapture).where(LLMPayloadCapture.user_id == user_id))
        ).scalar_one_or_none()
    assert row is not None
    assert row.current_era_request_id == "req-late"
    # The response must have landed despite the request committing later.
    assert row.current_era_response is not None
    assert row.current_era_response["content_blocks"][0]["text"] == "hello back"


@pytest.mark.asyncio()
async def test_response_capture_eventually_drops_when_request_never_arrives(
    async_db: async_sessionmaker,
) -> None:
    """When the retry budget is exhausted without a matching request row,
    the response is dropped (no partial row created)."""
    user_id = await _insert_user(async_db, consent=True)
    # No request capture for this request_id at all.
    await capture_llm_response(_make_response_payload(user_id=user_id, request_id="req-never"))

    row = await _read_capture(async_db, user_id)
    # No matching request -> no row to attach to.
    assert row is None


# Serialized size of a real production agent-main payload for an active user:
# a ~113k-token prompt plus 61 tool schemas and the system prompt. Hard-coded
# rather than derived from MAX_CAPTURE_BYTES, so this test keeps asserting
# against the real-world figure even if the cap moves again.
_OBSERVED_PRODUCTION_PAYLOAD_BYTES = 520_000


@pytest.mark.asyncio()
async def test_realistic_production_payload_is_captured(async_db: async_sessionmaker) -> None:
    """A normal heavy user must be captured, not dropped.

    The cap was originally 256 KB, below the size a single active user's prompt
    reaches well before OSS trims it. The result was that the payload-capture
    tool worked only for light users: the one user whose captures were wanted
    during an incident had logged "dropped (oversize)" on every call for two
    days. The cap is now sized off the trim ceiling instead.
    """
    user_id = await _insert_user(async_db, consent=True)

    chunk = "x" * (_OBSERVED_PRODUCTION_PAYLOAD_BYTES // 10)
    msgs = [{"role": "user", "content": chunk} for _ in range(10)]
    payload = _make_payload(user_id=user_id, min_seq=1, extra_messages=msgs)

    # Measured exactly the way the cap measures it, so the assertion below cannot
    # drift from what the production check actually compares.
    serialized_bytes = len(
        json.dumps(_payload_to_json_dict(payload), separators=(",", ":")).encode("utf-8")
    )
    assert serialized_bytes >= _OBSERVED_PRODUCTION_PAYLOAD_BYTES, (
        "test fixture no longer reproduces a production-sized payload"
    )

    await capture_llm_request(payload)

    row = await _read_capture(async_db, user_id)
    assert row is not None, "a production-sized payload was dropped"
    assert row.current_era_min_message_seq == 1


def test_cap_clears_the_oss_trim_ceiling() -> None:
    """The cap must sit above the prompt size OSS's default trim settings allow.

    OSS trims once the prompt passes ``context_trim_trigger_tokens`` and drops
    back to ``context_trim_target_tokens``, so those thresholds (times roughly 4
    bytes per token, plus tool schemas and the system prompt) set the size band a
    normal heavy user's payload lives in. Pinning the relationship here means a
    future change to either number has to be deliberate rather than silently
    reintroducing blanket drops.

    Read from the field *defaults*, not the live singleton: the trigger is
    settable via the ``CONTEXT_TRIM_TRIGGER_TOKENS`` env var (and OSS only warns,
    not rejects, up to ``max_input_tokens``), so asserting against the live value
    turns any operator or ``.env`` override into a confusing premium test failure
    that says nothing about premium code. The trigger is not in
    ``PERSISTABLE_SETTINGS``, so it cannot change at runtime either way.
    """
    approx_bytes_per_token = 4
    default_trigger_tokens = Settings.model_fields["context_trim_trigger_tokens"].default
    default_target_tokens = Settings.model_fields["context_trim_target_tokens"].default
    trim_ceiling_bytes = default_trigger_tokens * approx_bytes_per_token
    post_trim_resting_bytes = default_target_tokens * approx_bytes_per_token

    assert trim_ceiling_bytes < MAX_CAPTURE_BYTES, (
        f"MAX_CAPTURE_BYTES={MAX_CAPTURE_BYTES} is below the "
        f"~{trim_ceiling_bytes} byte prompt OSS allows before trimming; "
        "captures for active users will be dropped"
    )
    # The tighter bound, and the one the old 256 KB cap violated: every user who
    # has ever hit the trigger rests at ``context_trim_target_tokens`` from then
    # on, so a cap below this dropped 100% of their captures.
    assert post_trim_resting_bytes < MAX_CAPTURE_BYTES, (
        f"MAX_CAPTURE_BYTES={MAX_CAPTURE_BYTES} is below the "
        f"~{post_trim_resting_bytes} byte prompt a user rests at after OSS trims"
    )
