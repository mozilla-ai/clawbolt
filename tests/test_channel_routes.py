"""Tests for per-user channel enable/disable toggles (#821)."""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

import backend.app.database as _db_module
from backend.app.agent.heartbeat import resolve_heartbeat_route
from backend.app.agent.ingestion import InboundMessage, process_inbound_from_bus
from backend.app.bus import message_bus
from backend.app.models import ChannelRoute, User

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_route(user_id: str, channel: str, identifier: str, enabled: bool = True) -> None:
    db = _db_module.SessionLocal()
    try:
        db.add(
            ChannelRoute(
                user_id=user_id,
                channel=channel,
                channel_identifier=identifier,
                enabled=enabled,
            )
        )
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# API: GET /api/user/channels/routes
# ---------------------------------------------------------------------------


def test_get_routes_returns_enabled(client: TestClient, test_user: User) -> None:
    _create_route(test_user.id, "telegram", "111", enabled=True)
    _create_route(test_user.id, "linq", "222", enabled=False)

    resp = client.get("/api/user/channels/routes")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["routes"]) == 2
    by_channel = {r["channel"]: r for r in data["routes"]}
    assert by_channel["telegram"]["enabled"] is True
    assert by_channel["linq"]["enabled"] is False


def test_get_routes_empty(client: TestClient, test_user: User) -> None:
    resp = client.get("/api/user/channels/routes")
    assert resp.status_code == 200
    assert resp.json()["routes"] == []


# ---------------------------------------------------------------------------
# API: PATCH /api/user/channels/routes/{channel}
# ---------------------------------------------------------------------------


def test_patch_toggle_to_false(client: TestClient, test_user: User) -> None:
    _create_route(test_user.id, "telegram", "111", enabled=True)

    resp = client.patch(
        "/api/user/channels/routes/telegram",
        json={"enabled": False},
    )
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False


def test_patch_toggle_to_true(client: TestClient, test_user: User) -> None:
    _create_route(test_user.id, "telegram", "111", enabled=False)

    resp = client.patch(
        "/api/user/channels/routes/telegram",
        json={"enabled": True},
    )
    assert resp.status_code == 200
    assert resp.json()["enabled"] is True


def test_patch_creates_route_when_missing(client: TestClient, test_user: User) -> None:
    """PATCH on a channel with no identifier should persist the selection via
    preferred_channel without writing a placeholder ChannelRoute row.

    The UUID of the user was previously leaked into the row as a stand-in
    identifier and surfaced in the premium link UI. We now persist selection
    via User.preferred_channel instead, and create the row lazily once a
    real identifier (phone/email) is supplied.
    """
    # Simulate a fresh user who has never received an inbound message: no
    # channel_identifier, no existing routes.
    db = _db_module.SessionLocal()
    try:
        user = db.query(User).filter_by(id=test_user.id).first()
        assert user is not None
        user.channel_identifier = ""
        db.commit()
    finally:
        db.close()

    resp = client.patch(
        "/api/user/channels/routes/linq",
        json={"enabled": True},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["channel"] == "linq"
    assert body["enabled"] is True
    assert body["channel_identifier"] == ""

    # No row should have been created.
    db = _db_module.SessionLocal()
    try:
        rows = db.query(ChannelRoute).filter_by(user_id=test_user.id, channel="linq").all()
        assert rows == []
        user = db.query(User).filter_by(id=test_user.id).first()
        assert user is not None
        assert user.preferred_channel == "linq"
    finally:
        db.close()


def test_patch_creates_route_when_user_has_identifier(client: TestClient, test_user: User) -> None:
    """When the user has a real channel_identifier (e.g. from a prior inbound
    message), PATCH creates a row so the enabled flag persists alongside it."""
    db = _db_module.SessionLocal()
    try:
        user = db.query(User).filter_by(id=test_user.id).first()
        assert user is not None
        user.channel_identifier = "+15551234567"
        db.commit()
    finally:
        db.close()

    resp = client.patch(
        "/api/user/channels/routes/linq",
        json={"enabled": True},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["channel"] == "linq"
    assert body["enabled"] is True
    assert body["channel_identifier"] == "+15551234567"

    db = _db_module.SessionLocal()
    try:
        row = db.query(ChannelRoute).filter_by(user_id=test_user.id, channel="linq").first()
        assert row is not None
        assert row.channel_identifier == "+15551234567"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Inbound: disabled channel sends error reply
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_inbound_disabled_sends_error(test_user: User) -> None:
    _create_route(test_user.id, "telegram", "111", enabled=False)

    inbound = InboundMessage(
        channel="telegram",
        sender_id="111",
        text="hello",
        request_id="req-1",
    )

    with patch(
        "backend.app.agent.ingestion._get_or_create_user",
        new_callable=AsyncMock,
        return_value=test_user,
    ):
        await process_inbound_from_bus(inbound)

    assert message_bus.outbound_size == 1
    msg = await message_bus.consume_outbound()
    assert "currently disabled" in msg.content
    assert msg.channel == "telegram"


@pytest.mark.anyio
async def test_inbound_disabled_does_not_update_preferred(test_user: User) -> None:
    """When route is disabled, preferred_channel should not switch to it."""
    db = _db_module.SessionLocal()
    try:
        user = db.query(User).filter_by(id=test_user.id).first()
        assert user is not None
        user.preferred_channel = "linq"
        db.commit()
    finally:
        db.close()

    _create_route(test_user.id, "telegram", "111", enabled=False)

    inbound = InboundMessage(
        channel="telegram",
        sender_id="111",
        text="hello",
    )

    with patch("backend.app.agent.ingestion.settings.message_batch_window_ms", 0):
        await process_inbound_from_bus(inbound)

    db = _db_module.SessionLocal()
    try:
        user = db.query(User).filter_by(id=test_user.id).first()
        assert user is not None
        assert user.preferred_channel == "linq"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Heartbeat: skip disabled channels
# ---------------------------------------------------------------------------


def test_heartbeat_resolve_skips_disabled(test_user: User) -> None:
    """When preferred channel is disabled, fall back to next enabled."""
    _create_route(test_user.id, "telegram", "111", enabled=False)
    _create_route(test_user.id, "linq", "222", enabled=True)

    db = _db_module.SessionLocal()
    try:
        user = db.query(User).filter_by(id=test_user.id).first()
        assert user is not None
        user.preferred_channel = "telegram"
        db.commit()
        db.refresh(user)
        db.expunge(user)
    finally:
        db.close()

    with patch("backend.app.agent.heartbeat.get_channel"):
        db = _db_module.SessionLocal()
        try:
            result = resolve_heartbeat_route(user, db)
        finally:
            db.close()

    assert result is not None
    assert result[0] == "linq"


def test_heartbeat_resolve_is_pure_lookup(test_user: User) -> None:
    """resolve_heartbeat_route must never mutate persisted state.

    The write paths (PATCH, ingestion, startup migration, premium linking)
    own ``preferred_channel``. The heartbeat scheduler runs in a hot path on
    a detached User and should only read. If it drifts from an enabled route,
    that is a bug at the write path, not something to paper over here.
    """
    _create_route(test_user.id, "telegram", "111", enabled=False)
    _create_route(test_user.id, "linq", "222", enabled=True)

    db = _db_module.SessionLocal()
    try:
        user = db.query(User).filter_by(id=test_user.id).first()
        assert user is not None
        user.preferred_channel = "telegram"
        db.commit()
        db.refresh(user)
        db.expunge(user)
    finally:
        db.close()

    with patch("backend.app.agent.heartbeat.get_channel"):
        db = _db_module.SessionLocal()
        try:
            result = resolve_heartbeat_route(user, db)
        finally:
            db.close()

    assert result is not None
    assert result[0] == "linq"

    # preferred_channel must be untouched by the lookup.
    db = _db_module.SessionLocal()
    try:
        refreshed = db.query(User).filter_by(id=test_user.id).first()
        assert refreshed is not None
        assert refreshed.preferred_channel == "telegram"
    finally:
        db.close()


def test_heartbeat_resolve_none_when_all_disabled(test_user: User) -> None:
    """When all channels are disabled, return None."""
    _create_route(test_user.id, "telegram", "111", enabled=False)
    _create_route(test_user.id, "linq", "222", enabled=False)

    db = _db_module.SessionLocal()
    try:
        user = db.query(User).filter_by(id=test_user.id).first()
        assert user is not None
        user.preferred_channel = "telegram"
        db.commit()
        db.refresh(user)
        db.expunge(user)
    finally:
        db.close()

    db = _db_module.SessionLocal()
    try:
        result = resolve_heartbeat_route(user, db)
    finally:
        db.close()

    assert result is None


# ---------------------------------------------------------------------------
# last_inbound_at: stamped on every inbound routing
# ---------------------------------------------------------------------------


def test_last_inbound_at_null_until_first_inbound(client: TestClient, test_user: User) -> None:
    """Newly-created route reports last_inbound_at=None until a message arrives."""
    _create_route(test_user.id, "telegram", "111", enabled=True)

    resp = client.get("/api/user/channels/routes")
    assert resp.status_code == 200
    routes = resp.json()["routes"]
    assert len(routes) == 1
    assert routes[0]["last_inbound_at"] is None


@pytest.mark.asyncio
async def test_last_inbound_at_stamped_by_ingestion(client: TestClient, test_user: User) -> None:
    """An inbound message resolving to a route updates last_inbound_at so the
    channel picker UI can flip to "Verified"."""
    _create_route(test_user.id, "telegram", "111", enabled=True)

    with patch.object(message_bus, "publish_outbound", new_callable=AsyncMock):
        await process_inbound_from_bus(
            InboundMessage(
                channel="telegram",
                sender_id="111",
                text="hello",
                session_id="test-session",
                external_message_id="ext-1",
                media_refs=[],
            )
        )

    resp = client.get("/api/user/channels/routes")
    assert resp.status_code == 200
    routes = resp.json()["routes"]
    stamped = next((r for r in routes if r["channel"] == "telegram"), None)
    assert stamped is not None
    assert stamped["last_inbound_at"] is not None
