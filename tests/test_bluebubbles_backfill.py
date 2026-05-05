"""Tests for BlueBubbles startup backfill.

Covers the gap between the live webhook (fire-and-forget from BlueBubbles)
and the orphan inbound recovery sweep (which only handles DB-persisted
messages whose pipeline crashed): messages that arrived during a
Clawbolt outage and never reached our DB at all.

The backfill queries the BlueBubbles server for messages dated in the
last N minutes and replays each through ``handle_webhook_inbound``. The
existing idempotency store dedups anything we already saw via the live
webhook, so these tests assert both the replay-on-outage path and the
no-op-on-healthy-boot path.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.app.channels.bluebubbles import (
    _BACKFILL_LOCK_KEY,
    BlueBubblesChannel,
    _derive_webhook_token,
    _release_backfill_lock,
    _try_acquire_backfill_lock,
)
from tests.mocks.bluebubbles import make_bluebubbles_webhook_payload

_PATCH_BUS_PUBLISH = "backend.app.bus.message_bus.publish_inbound"

_ASYNC_TEST_DB_URL = "postgresql+asyncpg://clawbolt:clawbolt@localhost:5432/clawbolt_test"


def _make_query_response(messages: list[dict[str, Any]], status_code: int = 200) -> MagicMock:
    """Build a mock httpx.Response for /api/v1/message/query."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json = MagicMock(return_value={"data": messages, "metadata": {"count": len(messages)}})
    resp.text = ""
    return resp


def _query_message(
    sender: str = "+15551234567",
    text: str = "missed this one",
    message_guid: str = "missed-001",
    is_from_me: bool = False,
) -> dict[str, Any]:
    """A single message in the shape /api/v1/message/query returns.

    Matches the webhook payload's ``data`` field shape exactly: BlueBubbles
    serializes the same way for both, which is why a single Pydantic model
    parses both.
    """
    payload = make_bluebubbles_webhook_payload(
        sender=sender,
        text=text,
        message_guid=message_guid,
        is_from_me=is_from_me,
    )
    return payload["data"]


# ---------------------------------------------------------------------------
# Disabled / unconfigured short-circuits
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_lookback_returns_zero_without_http() -> None:
    """``bluebubbles_backfill_lookback_minutes=0`` short-circuits before HTTP."""
    channel = BlueBubblesChannel()
    with (
        patch("backend.app.channels.bluebubbles.settings.bluebubbles_server_url", "https://x"),
        patch("backend.app.channels.bluebubbles.settings.bluebubbles_password", "pw"),
        patch(
            "backend.app.channels.bluebubbles.settings.bluebubbles_backfill_lookback_minutes",
            0,
        ),
        patch.object(BlueBubblesChannel, "_http", new=MagicMock()) as mock_http,
    ):
        result = await channel.run_startup_backfill()

    assert result == 0
    mock_http.post.assert_not_called()


@pytest.mark.asyncio
async def test_unconfigured_returns_zero_without_http() -> None:
    """No server_url or password short-circuits before HTTP."""
    channel = BlueBubblesChannel()
    with (
        patch("backend.app.channels.bluebubbles.settings.bluebubbles_server_url", ""),
        patch("backend.app.channels.bluebubbles.settings.bluebubbles_password", ""),
        patch.object(BlueBubblesChannel, "_http", new=MagicMock()) as mock_http,
    ):
        result = await channel.run_startup_backfill()

    assert result == 0
    mock_http.post.assert_not_called()


# ---------------------------------------------------------------------------
# Replay path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missed_message_is_replayed(bluebubbles_client: object, async_db: object) -> None:
    """A message returned by /api/v1/message/query is replayed onto the bus.

    The ``bluebubbles_client`` fixture is here to set up settings, allowlist,
    and reset state. We don't use the TestClient itself. The ``async_db``
    fixture rebinds the module-level async session factory to a per-test
    connection so the backfill's advisory-lock SQL runs inside the
    rolled-back outer transaction. See ``tests/conftest.py``.
    """
    channel = BlueBubblesChannel()
    msg = _query_message(text="hello after outage", message_guid="missed-001")

    fake_client = MagicMock()
    fake_client.post = AsyncMock(return_value=_make_query_response([msg]))

    with (
        patch("backend.app.channels.bluebubbles.settings.bluebubbles_server_url", "https://x"),
        patch("backend.app.channels.bluebubbles.settings.bluebubbles_password", "pw"),
        patch(
            "backend.app.channels.bluebubbles.settings.bluebubbles_backfill_lookback_minutes",
            30,
        ),
        patch("backend.app.channels.bluebubbles.settings.bluebubbles_allowed_numbers", "*"),
        patch.object(BlueBubblesChannel, "_http", new=fake_client),
        patch(_PATCH_BUS_PUBLISH, new_callable=AsyncMock) as mock_pub,
    ):
        result = await channel.run_startup_backfill()

    assert result == 1
    mock_pub.assert_called_once()
    inbound = mock_pub.call_args[0][0]
    assert inbound.channel == "bluebubbles"
    assert inbound.text == "hello after outage"
    assert inbound.external_message_id == "bb_missed-001"


@pytest.mark.asyncio
async def test_is_from_me_messages_are_skipped(
    bluebubbles_client: object, async_db: object
) -> None:
    """Outgoing messages echoed back by the query API are not replayed."""
    channel = BlueBubblesChannel()
    incoming = _query_message(text="from contact", message_guid="in-1")
    outgoing = _query_message(text="from me", message_guid="out-1", is_from_me=True)

    fake_client = MagicMock()
    fake_client.post = AsyncMock(return_value=_make_query_response([incoming, outgoing]))

    with (
        patch("backend.app.channels.bluebubbles.settings.bluebubbles_server_url", "https://x"),
        patch("backend.app.channels.bluebubbles.settings.bluebubbles_password", "pw"),
        patch(
            "backend.app.channels.bluebubbles.settings.bluebubbles_backfill_lookback_minutes",
            30,
        ),
        patch("backend.app.channels.bluebubbles.settings.bluebubbles_allowed_numbers", "*"),
        patch.object(BlueBubblesChannel, "_http", new=fake_client),
        patch(_PATCH_BUS_PUBLISH, new_callable=AsyncMock) as mock_pub,
    ):
        result = await channel.run_startup_backfill()

    assert result == 1
    mock_pub.assert_called_once()
    assert mock_pub.call_args[0][0].external_message_id == "bb_in-1"


@pytest.mark.asyncio
async def test_already_seen_messages_are_deduped(
    bluebubbles_client: object, async_db: object
) -> None:
    """Messages we already processed via the live webhook are not re-replayed.

    Idempotency is enforced by ``IdempotencyStore.try_mark_seen`` keyed off
    ``external_message_id``. Calling backfill twice for the same message
    must publish only once.
    """
    channel = BlueBubblesChannel()
    msg = _query_message(text="dup test", message_guid="dup-001")

    fake_client = MagicMock()
    fake_client.post = AsyncMock(return_value=_make_query_response([msg]))

    with (
        patch("backend.app.channels.bluebubbles.settings.bluebubbles_server_url", "https://x"),
        patch("backend.app.channels.bluebubbles.settings.bluebubbles_password", "pw"),
        patch(
            "backend.app.channels.bluebubbles.settings.bluebubbles_backfill_lookback_minutes",
            30,
        ),
        patch("backend.app.channels.bluebubbles.settings.bluebubbles_allowed_numbers", "*"),
        patch.object(BlueBubblesChannel, "_http", new=fake_client),
        patch(_PATCH_BUS_PUBLISH, new_callable=AsyncMock) as mock_pub,
    ):
        await channel.run_startup_backfill()
        await channel.run_startup_backfill()

    assert mock_pub.call_count == 1


@pytest.mark.asyncio
async def test_webhook_processed_message_is_not_replayed_after_restart(
    bluebubbles_client: TestClient,
    async_db: object,
) -> None:
    """End-to-end production scenario: the live webhook delivered a message,
    the agent replied, the server later restarts, and the BlueBubbles query
    still returns that message in its lookback window. The user must NOT be
    re-pinged.

    This is the critical safety check: if dedup ever broke, restarts would
    spam users with replies to messages already handled. We exercise the
    real webhook path (idempotency_keys row written via ``try_mark_seen``)
    and only then run the backfill, asserting zero additional bus publishes.
    """
    channel = BlueBubblesChannel()
    raw_message = _query_message(text="already replied to", message_guid="prior-reply-001")
    webhook_payload = {"type": "new-message", "data": raw_message}

    fake_client = MagicMock()
    fake_client.post = AsyncMock(return_value=_make_query_response([raw_message]))

    # Real password is required: backfill short-circuits when
    # ``bluebubbles_password`` is empty, which would silently neuter
    # this test (it would pass trivially without ever invoking dedup).
    password = "test-password"
    token = _derive_webhook_token(password)

    with (
        patch("backend.app.channels.bluebubbles.settings.bluebubbles_server_url", "https://x"),
        patch("backend.app.channels.bluebubbles.settings.bluebubbles_password", password),
        patch(
            "backend.app.channels.bluebubbles.settings.bluebubbles_backfill_lookback_minutes",
            30,
        ),
        patch("backend.app.channels.bluebubbles.settings.bluebubbles_allowed_numbers", "*"),
        patch.object(BlueBubblesChannel, "_http", new=fake_client),
        patch(_PATCH_BUS_PUBLISH, new_callable=AsyncMock) as mock_pub,
    ):
        # Step 1: live webhook delivers the message via the running app.
        # Goes through the real router -> handle_webhook_inbound ->
        # IdempotencyStore.try_mark_seen, which commits the seen-row.
        webhook_resp = bluebubbles_client.post(
            f"/api/webhooks/bluebubbles?token={token}", json=webhook_payload
        )
        assert webhook_resp.status_code == 200
        assert mock_pub.call_count == 1, "live webhook should publish to bus on first delivery"

        # Step 2: server restarts. Backfill runs against the same
        # BlueBubbles server, which still has the message in its
        # lookback window. Idempotency must reject it.
        replayed = await channel.run_startup_backfill()

    # Backfill saw 1 message and ran it through handle_webhook_inbound,
    # so its return value (attempted-before-dedup) is 1; the bus publish
    # count is what protects against re-pinging the user.
    assert replayed == 1, "backfill should have attempted the message (then deduped)"
    fake_client.post.assert_called_once()  # backfill actually ran the query
    assert mock_pub.call_count == 1, (
        "backfill replayed a message the live webhook already handled; "
        "users would be re-pinged with stale replies on every restart"
    )


@pytest.mark.asyncio
async def test_chat_guid_is_cached_for_outbound_replies(
    bluebubbles_client: object, async_db: object
) -> None:
    """Backfill populates the chat-guid cache so replies don't reconstruct it."""
    channel = BlueBubblesChannel()
    msg = _query_message(message_guid="cache-001")
    msg["chats"] = [{"guid": "iMessage;-;groupchat-abc"}]

    fake_client = MagicMock()
    fake_client.post = AsyncMock(return_value=_make_query_response([msg]))

    with (
        patch("backend.app.channels.bluebubbles.settings.bluebubbles_server_url", "https://x"),
        patch("backend.app.channels.bluebubbles.settings.bluebubbles_password", "pw"),
        patch(
            "backend.app.channels.bluebubbles.settings.bluebubbles_backfill_lookback_minutes",
            30,
        ),
        patch("backend.app.channels.bluebubbles.settings.bluebubbles_allowed_numbers", "*"),
        patch.object(BlueBubblesChannel, "_http", new=fake_client),
        patch(_PATCH_BUS_PUBLISH, new_callable=AsyncMock),
    ):
        await channel.run_startup_backfill()

    assert channel._chat_cache.get("+15551234567") == "iMessage;-;groupchat-abc"


# ---------------------------------------------------------------------------
# Failure modes don't block startup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_error_returns_zero_without_raising(
    bluebubbles_client: object, async_db: object
) -> None:
    """A wedged BlueBubbles server cannot block app startup."""
    channel = BlueBubblesChannel()

    fake_client = MagicMock()
    fake_client.post = AsyncMock(side_effect=httpx.ConnectError("server down"))

    with (
        patch("backend.app.channels.bluebubbles.settings.bluebubbles_server_url", "https://x"),
        patch("backend.app.channels.bluebubbles.settings.bluebubbles_password", "pw"),
        patch(
            "backend.app.channels.bluebubbles.settings.bluebubbles_backfill_lookback_minutes",
            30,
        ),
        patch.object(BlueBubblesChannel, "_http", new=fake_client),
        patch(_PATCH_BUS_PUBLISH, new_callable=AsyncMock) as mock_pub,
    ):
        result = await channel.run_startup_backfill()

    assert result == 0
    mock_pub.assert_not_called()


@pytest.mark.asyncio
async def test_4xx_response_returns_zero(bluebubbles_client: object, async_db: object) -> None:
    """A 4xx (e.g. wrong password) is logged but does not block startup."""
    channel = BlueBubblesChannel()

    fake_client = MagicMock()
    fake_client.post = AsyncMock(return_value=_make_query_response([], status_code=401))

    with (
        patch("backend.app.channels.bluebubbles.settings.bluebubbles_server_url", "https://x"),
        patch("backend.app.channels.bluebubbles.settings.bluebubbles_password", "pw"),
        patch(
            "backend.app.channels.bluebubbles.settings.bluebubbles_backfill_lookback_minutes",
            30,
        ),
        patch.object(BlueBubblesChannel, "_http", new=fake_client),
        patch(_PATCH_BUS_PUBLISH, new_callable=AsyncMock) as mock_pub,
    ):
        result = await channel.run_startup_backfill()

    assert result == 0
    mock_pub.assert_not_called()


@pytest.mark.asyncio
async def test_empty_response_returns_zero(bluebubbles_client: object, async_db: object) -> None:
    """No messages in the lookback window is the common healthy-boot case."""
    channel = BlueBubblesChannel()

    fake_client = MagicMock()
    fake_client.post = AsyncMock(return_value=_make_query_response([]))

    with (
        patch("backend.app.channels.bluebubbles.settings.bluebubbles_server_url", "https://x"),
        patch("backend.app.channels.bluebubbles.settings.bluebubbles_password", "pw"),
        patch(
            "backend.app.channels.bluebubbles.settings.bluebubbles_backfill_lookback_minutes",
            30,
        ),
        patch.object(BlueBubblesChannel, "_http", new=fake_client),
        patch(_PATCH_BUS_PUBLISH, new_callable=AsyncMock) as mock_pub,
    ):
        result = await channel.run_startup_backfill()

    assert result == 0
    mock_pub.assert_not_called()


# ---------------------------------------------------------------------------
# Query parameters
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lookback_minutes_drives_after_param(
    bluebubbles_client: object, async_db: object
) -> None:
    """The ``after`` parameter is now-minus-lookback in unix milliseconds."""
    channel = BlueBubblesChannel()

    fake_client = MagicMock()
    fake_client.post = AsyncMock(return_value=_make_query_response([]))

    with (
        patch("backend.app.channels.bluebubbles.settings.bluebubbles_server_url", "https://x"),
        patch("backend.app.channels.bluebubbles.settings.bluebubbles_password", "pw"),
        patch(
            "backend.app.channels.bluebubbles.settings.bluebubbles_backfill_lookback_minutes",
            45,
        ),
        patch.object(BlueBubblesChannel, "_http", new=fake_client),
    ):
        await channel.run_startup_backfill()

    fake_client.post.assert_called_once()
    call_kwargs = fake_client.post.call_args.kwargs
    body = call_kwargs["json"]
    assert body["sort"] == "ASC"
    assert "chat" in body["with"]
    # ``after`` should be roughly now - 45 minutes in ms. Allow generous slack
    # for clock drift inside the test runner.
    expected_ms = int((time.time() - 45 * 60) * 1000)
    assert abs(body["after"] - expected_ms) < 60_000  # within 60s


# ---------------------------------------------------------------------------
# Connection-pinning regression: backfill advisory lock (issue #1176, PR #1212)
# ---------------------------------------------------------------------------


class TestBackfillLockConnectionPinning:
    """Regression tests for the BlueBubbles backfill advisory lock.

    The lock around ``run_startup_backfill`` exists so two workers
    booting at the same moment (rolling restart, K8s replica scale-out)
    do not both replay the same lookback window: the first acquires,
    runs the replay, releases; the second sees the lock held, logs, and
    skips. The idempotency store catches dup deliveries inside any
    single worker, but two workers running the replay in parallel still
    waste an extra round-trip per message and put unnecessary load on
    the BlueBubbles server.

    Mirrors ``test_oauth_refresh.py::TestRefreshTokenLockSerialization``
    and ``test_inbound_recovery.py::test_unlock_on_different_connection_is_a_no_op``
    for the backfill helper. Encodes two invariants:

    1. The lock helper's connection-pinning contract: ``pg_advisory_unlock``
       on a different connection silently no-ops, so the helpers MUST
       be called on a dedicated ``AsyncConnection`` that the caller
       holds across acquire + critical section + release. An
       ``AsyncSession`` is wrong because ``AsyncSession.commit()``
       returns the connection to the pool (where a peer can re-acquire
       under the same PG session because advisory locks are reentrant).

    2. The new ``AsyncConnection``-based helpers actually work: the
       lock is exclusive across two callers contending on a constrained
       pool, and the unlock on the holder connection lets the second
       caller acquire.
    """

    @pytest.mark.asyncio()
    async def test_unlock_on_different_async_connection_is_a_no_op(self) -> None:
        """``pg_advisory_unlock`` on a different ``AsyncConnection`` is a
        silent no-op: the lock stays held by the original connection.

        This is the production bug encoded as an invariant. PR #1212
        originally took the lock on an ``AsyncSession``; ``commit()``
        between acquire and unlock returned the underlying connection
        to the pool, so the unlock SQL ran on whatever connection the
        next ``execute`` checked out (usually a different one), and the
        original lock leaked until the holder connection eventually
        closed.

        If a future refactor re-introduces an ``AsyncSession`` (or any
        handle whose ``commit()`` recycles the connection), the
        same-connection coupling breaks again. This test fails
        immediately in that case.
        """
        engine = create_async_engine(_ASYNC_TEST_DB_URL, pool_pre_ping=True)
        try:
            holder_conn = await engine.connect()
            wrong_conn = await engine.connect()
            observer_conn = await engine.connect()
            try:
                # Holder acquires on its own connection.
                acquired = await _try_acquire_backfill_lock(holder_conn)
                assert acquired is True

                # Try to release on a different connection. Postgres
                # returns False here (lock not owned by this session).
                # Our helper swallows the result, so we run the SQL
                # directly to inspect the return value.
                result = await wrong_conn.execute(
                    text("SELECT pg_advisory_unlock(hashtext(:k))"),
                    {"k": _BACKFILL_LOCK_KEY},
                )
                unlocked = bool(result.scalar())
                assert unlocked is False, (
                    "pg_advisory_unlock returned True on a non-owning "
                    "connection; Postgres semantics changed and this "
                    "test needs updating"
                )

                # The lock is still held: a third connection cannot
                # acquire. With the bug (pre-fix), this would be True
                # because the holder's lock leaked to a recycled
                # connection that another caller picked up.
                still_held = not await _try_acquire_backfill_lock(observer_conn)
                assert still_held, (
                    "lock was released by an unlock on a different "
                    "connection; the backfill code's same-connection "
                    "coupling is broken"
                )

                # Releasing on the holder connection actually frees it.
                await _release_backfill_lock(holder_conn)
                now_free = await _try_acquire_backfill_lock(observer_conn)
                assert now_free is True, "lock did not free after release on the owning connection"
                await _release_backfill_lock(observer_conn)
            finally:
                await holder_conn.close()
                await wrong_conn.close()
                await observer_conn.close()
        finally:
            await engine.dispose()

    @pytest.mark.asyncio()
    async def test_run_startup_backfill_serializes_concurrent_workers(self) -> None:
        """Mutation-test invariant for the production caller.

        Two concurrent ``run_startup_backfill`` calls on a small pool
        (``pool_size=1, max_overflow=0`` forces connection recycling
        between the two callers' DB handles): with the fix, only one
        caller's HTTP query runs because the other sees the lock held
        on the dedicated ``lock_conn`` and skips. With the bug (lock
        held on an ``AsyncSession`` whose ``commit()`` between
        ``pg_try_advisory_lock`` and the critical section recycled the
        connection back into the pool), both callers' sessions
        successively pick up the same physical PG connection,
        ``pg_try_advisory_lock`` returns True for both (locks are
        reentrant per PG session), and both run the HTTP query.

        We pin the production module-level engine to a ``pool_size=1,
        max_overflow=0`` engine so ``get_async_engine().connect()`` and
        ``AsyncSessionLocal()`` both pull from the same constrained pool.
        The first acquirer must release before the second can run, which
        is the exact serialization contract the lock guarantees.

        We block inside the critical section (the mocked HTTP call) so
        both callers are simultaneously inside ``run_startup_backfill``,
        forcing them to actually contend on the lock rather than
        running sequentially because of GIL scheduling.
        """
        from backend.app import database as _db_module

        engine = create_async_engine(
            _ASYNC_TEST_DB_URL, pool_size=1, max_overflow=0, pool_pre_ping=True
        )
        old_async_engine = _db_module._async_engine
        old_async_factory = _db_module._async_session_factory
        _db_module._async_engine = engine
        _db_module._async_session_factory = async_sessionmaker(
            bind=engine, autoflush=False, expire_on_commit=False
        )

        # Channel under test. Each test gets its own instance so the
        # ``_chat_cache`` and ``_http`` patches don't leak.
        channel_a = BlueBubblesChannel()
        channel_b = BlueBubblesChannel()

        # The HTTP call that runs inside the critical section. Both
        # callers share the same Event so we can hold them both inside
        # the section simultaneously and prove the serialization
        # contract (only one should reach the HTTP call).
        import asyncio

        release = asyncio.Event()
        call_count = 0
        call_count_lock = asyncio.Lock()

        async def _slow_post(*args: object, **kwargs: object) -> Any:
            nonlocal call_count
            async with call_count_lock:
                call_count += 1
            await release.wait()
            return _make_query_response([])

        fake_http = MagicMock()
        fake_http.post = AsyncMock(side_effect=_slow_post)

        try:
            with (
                patch(
                    "backend.app.channels.bluebubbles.settings.bluebubbles_server_url",
                    "https://x",
                ),
                patch(
                    "backend.app.channels.bluebubbles.settings.bluebubbles_password",
                    "pw",
                ),
                patch(
                    "backend.app.channels.bluebubbles.settings.bluebubbles_backfill_lookback_minutes",
                    30,
                ),
                # ``_http`` is a class-level property; patch on the
                # class so both channel instances see the same mock.
                patch.object(BlueBubblesChannel, "_http", new=fake_http),
            ):
                # Start A first so it deterministically wins the lock.
                # B's call should observe the lock held and skip
                # without invoking _slow_post at all.
                task_a = asyncio.create_task(channel_a.run_startup_backfill())

                # Wait until A is inside the critical section (its HTTP
                # call has been entered). Without this, B might race A
                # to ``pg_try_advisory_lock`` before A has acquired.
                async def _wait_for_a() -> None:
                    deadline = time.monotonic() + 5.0
                    while call_count < 1 and time.monotonic() < deadline:
                        await asyncio.sleep(0.02)

                await _wait_for_a()
                assert call_count == 1, (
                    "worker A did not enter the critical section; test setup is broken"
                )

                # Now launch B. With the fix, B finds the lock held
                # and skips before reaching the HTTP call. With the
                # bug, B's AsyncSession picks up the recycled
                # connection and pg_try_advisory_lock returns True
                # (reentrant per PG session), so B also enters the
                # HTTP call.
                task_b = asyncio.create_task(channel_b.run_startup_backfill())

                # Give B time to either skip (fix) or also reach the
                # HTTP call (bug). 0.3s is generous on a fast machine
                # and small enough to keep the test snappy.
                await asyncio.sleep(0.3)

                # Snapshot before releasing A. With the fix, B saw the
                # lock held and skipped before reaching the HTTP call;
                # with the bug, B's session reused the recycled
                # connection, ``pg_try_advisory_lock`` returned True,
                # and B reached the HTTP call (incrementing the
                # counter to 2). Capturing the count BEFORE A finishes
                # is what makes this a true mutation witness: after
                # release, B's lock acquire would succeed for an
                # entirely benign reason (A released first), so the
                # post-release count would always be 2 even on the
                # fixed code.
                in_section_count = call_count
                released_at_count = call_count

                # Now release A so the test can finish.
                release.set()
                await asyncio.wait_for(asyncio.gather(task_a, task_b), timeout=5.0)

            # The acid test: at the moment release.set() fired, exactly
            # one HTTP call was in flight. With the fix, B's
            # ``_try_acquire_backfill_lock`` returned False because A
            # held the lock on its dedicated ``AsyncConnection`` and
            # the pool could not satisfy B's connect() until A closed
            # lock_conn. With the bug (lock held on AsyncSession),
            # A's commit between acquire and HTTP returned the
            # connection to the pool; B's session pulled it out;
            # ``pg_try_advisory_lock`` returned True (reentrant per PG
            # session); B entered the HTTP call concurrently.
            assert in_section_count == 1, (
                f"expected exactly one backfill HTTP call in flight "
                f"while worker A held the lock, got {in_section_count}; "
                f"the advisory lock failed to serialize concurrent "
                f"backfills (the lock was held on a recycled "
                f"connection, not the dedicated lock connection)"
            )
            assert released_at_count == 1
        finally:
            # Cleanup: drop any leaked locks, restore module state.
            async with engine.connect() as cleanup:
                await cleanup.execute(text("SELECT pg_advisory_unlock_all()"))
            await engine.dispose()
            _db_module._async_engine = old_async_engine
            _db_module._async_session_factory = old_async_factory

    @pytest.mark.asyncio()
    async def test_async_connection_pinning_serializes_two_workers(self) -> None:
        """Positive proof: with the ``AsyncConnection``-based helpers,
        two callers contending on the lock are correctly serialized.

        Worker A acquires, holds. Worker B's acquire returns False
        because the lock is held by another PG session. After A releases,
        B can acquire. This is the steady-state behavior production
        relies on for rolling restarts.
        """
        engine = create_async_engine(_ASYNC_TEST_DB_URL, pool_pre_ping=True)
        try:
            conn_a = await engine.connect()
            conn_b = await engine.connect()
            try:
                # A acquires.
                got_a = await _try_acquire_backfill_lock(conn_a)
                assert got_a is True

                # B contends and gets False.
                got_b = await _try_acquire_backfill_lock(conn_b)
                assert got_b is False, (
                    "two concurrent backfill callers both acquired the lock; "
                    "the advisory lock is no longer exclusive across PG sessions"
                )

                # A releases.
                await _release_backfill_lock(conn_a)

                # B can now acquire.
                got_b_after = await _try_acquire_backfill_lock(conn_b)
                assert got_b_after is True, (
                    "lock did not free after release on the owning connection"
                )
                await _release_backfill_lock(conn_b)
            finally:
                await conn_a.close()
                await conn_b.close()
        finally:
            await engine.dispose()
