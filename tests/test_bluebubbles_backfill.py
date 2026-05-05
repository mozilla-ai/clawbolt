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

from backend.app.channels.bluebubbles import BlueBubblesChannel, _derive_webhook_token
from tests.mocks.bluebubbles import make_bluebubbles_webhook_payload

_PATCH_BUS_PUBLISH = "backend.app.bus.message_bus.publish_inbound"


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
