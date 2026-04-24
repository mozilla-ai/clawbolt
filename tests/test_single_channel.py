"""Tests for single-channel enforcement.

Verifies that enabling a channel auto-disables other non-webchat channels
and updates preferred_channel. Also tests guard rails (webchat rejection,
unknown channel rejection) and startup migration.
"""

import uuid

import backend.app.database as _db_module
from backend.app.agent.heartbeat import resolve_heartbeat_route
from backend.app.agent.ingestion import _get_or_create_user
from backend.app.models import ChannelRoute, User


def _create_user_with_routes(
    routes: list[tuple[str, str, bool]],
    preferred_channel: str = "telegram",
) -> str:
    """Helper: create a user with the given routes, return user_id.

    ``routes`` is a list of (channel, identifier, enabled) tuples.
    """
    db = _db_module.SessionLocal()
    try:
        user = User(
            id=str(uuid.uuid4()),
            user_id=f"test-{uuid.uuid4().hex[:8]}",
            channel_identifier=routes[0][1] if routes else "",
            preferred_channel=preferred_channel,
            onboarding_complete=True,
        )
        db.add(user)
        db.flush()
        for channel, identifier, enabled in routes:
            db.add(
                ChannelRoute(
                    user_id=user.id,
                    channel=channel,
                    channel_identifier=identifier,
                    enabled=enabled,
                )
            )
        db.commit()
        uid = user.id
    finally:
        db.close()
    return uid


class TestAutoDisableOnEnable:
    """PATCH /user/channels/routes/{channel} with enabled=true disables others."""

    def test_enable_telegram_disables_linq(self, client) -> None:  # noqa: ANN001
        # Get the test user's ID
        db = _db_module.SessionLocal()
        try:
            user = db.query(User).first()
            assert user is not None
            user_id = user.id
            db.add(
                ChannelRoute(
                    user_id=user_id,
                    channel="telegram",
                    channel_identifier="111",
                    enabled=True,
                )
            )
            db.add(
                ChannelRoute(
                    user_id=user_id,
                    channel="linq",
                    channel_identifier="+15551234567",
                    enabled=True,
                )
            )
            db.commit()
        finally:
            db.close()

        resp = client.patch(
            "/api/user/channels/routes/telegram",
            json={"enabled": True},
        )
        assert resp.status_code == 200
        assert resp.json()["enabled"] is True

        db = _db_module.SessionLocal()
        try:
            linq_route = db.query(ChannelRoute).filter_by(user_id=user_id, channel="linq").first()
            assert linq_route is not None
            assert linq_route.enabled is False
        finally:
            db.close()

    def test_enable_preserves_webchat(self, client) -> None:  # noqa: ANN001
        db = _db_module.SessionLocal()
        try:
            user = db.query(User).first()
            assert user is not None
            user_id = user.id
            db.add(
                ChannelRoute(
                    user_id=user_id,
                    channel="webchat",
                    channel_identifier=user_id,
                    enabled=True,
                )
            )
            db.add(
                ChannelRoute(
                    user_id=user_id,
                    channel="telegram",
                    channel_identifier="111",
                    enabled=True,
                )
            )
            db.commit()
        finally:
            db.close()

        resp = client.patch(
            "/api/user/channels/routes/telegram",
            json={"enabled": True},
        )
        assert resp.status_code == 200

        db = _db_module.SessionLocal()
        try:
            webchat_route = (
                db.query(ChannelRoute).filter_by(user_id=user_id, channel="webchat").first()
            )
            assert webchat_route is not None
            assert webchat_route.enabled is True
        finally:
            db.close()

    def test_enable_updates_preferred_channel(self, client) -> None:  # noqa: ANN001
        db = _db_module.SessionLocal()
        try:
            user = db.query(User).first()
            assert user is not None
            user_id = user.id
            user.preferred_channel = "linq"
            db.commit()
        finally:
            db.close()

        resp = client.patch(
            "/api/user/channels/routes/telegram",
            json={"enabled": True},
        )
        assert resp.status_code == 200

        db = _db_module.SessionLocal()
        try:
            user = db.query(User).filter_by(id=user_id).first()
            assert user is not None
            assert user.preferred_channel == "telegram"
        finally:
            db.close()

    def test_disable_does_not_affect_others(self, client) -> None:  # noqa: ANN001
        db = _db_module.SessionLocal()
        try:
            user = db.query(User).first()
            assert user is not None
            user_id = user.id
            db.add(
                ChannelRoute(
                    user_id=user_id,
                    channel="telegram",
                    channel_identifier="111",
                    enabled=True,
                )
            )
            db.add(
                ChannelRoute(
                    user_id=user_id,
                    channel="linq",
                    channel_identifier="+15551234567",
                    enabled=True,
                )
            )
            db.commit()
        finally:
            db.close()

        resp = client.patch(
            "/api/user/channels/routes/telegram",
            json={"enabled": False},
        )
        assert resp.status_code == 200

        db = _db_module.SessionLocal()
        try:
            linq_route = db.query(ChannelRoute).filter_by(user_id=user_id, channel="linq").first()
            assert linq_route is not None
            assert linq_route.enabled is True
        finally:
            db.close()


class TestGuardRails:
    """Validation of channel name in PATCH endpoint."""

    def test_reject_webchat_enable(self, client) -> None:  # noqa: ANN001
        resp = client.patch(
            "/api/user/channels/routes/webchat",
            json={"enabled": True},
        )
        assert resp.status_code == 400
        assert "webchat" in resp.json()["detail"].lower()

    def test_reject_unknown_channel(self, client) -> None:  # noqa: ANN001
        resp = client.patch(
            "/api/user/channels/routes/nonexistent_channel",
            json={"enabled": True},
        )
        assert resp.status_code == 404
        assert "unknown" in resp.json()["detail"].lower()


class TestIngestionNoAutoDisable:
    """Inbound messages must NOT auto-disable other channels."""

    async def test_inbound_does_not_disable_existing_routes(self) -> None:
        user_id = _create_user_with_routes(
            [
                ("telegram", "ingest-111", True),
                ("linq", "+15559999999", True),
            ],
            preferred_channel="telegram",
        )

        resolved_user = await _get_or_create_user("telegram", "ingest-111")
        assert resolved_user.id == user_id

        db = _db_module.SessionLocal()
        try:
            linq_route = db.query(ChannelRoute).filter_by(user_id=user_id, channel="linq").first()
            assert linq_route is not None
            assert linq_route.enabled is True
        finally:
            db.close()

    async def test_webchat_does_not_overwrite_preferred_channel(self) -> None:
        uid = str(uuid.uuid4())
        db = _db_module.SessionLocal()
        try:
            user = User(
                id=uid,
                user_id=f"wc-test-{uuid.uuid4().hex[:8]}",
                channel_identifier="wc-111",
                preferred_channel="telegram",
                onboarding_complete=True,
            )
            db.add(user)
            db.flush()
            db.add(
                ChannelRoute(
                    user_id=uid,
                    channel="telegram",
                    channel_identifier="wc-111",
                    enabled=True,
                )
            )
            db.add(
                ChannelRoute(
                    user_id=uid,
                    channel="webchat",
                    channel_identifier=uid,
                    enabled=True,
                )
            )
            db.commit()
        finally:
            db.close()

        resolved_user = await _get_or_create_user("webchat", uid)
        assert resolved_user.id == uid

        db = _db_module.SessionLocal()
        try:
            u = db.query(User).filter_by(id=uid).first()
            assert u is not None
            assert u.preferred_channel == "telegram"
        finally:
            db.close()


class TestStaleRouteCleanup:
    """Stale route cleanup on inbound message."""

    async def test_stale_route_removed_on_inbound(self) -> None:
        """When a user sends from a new identifier, stale routes for that
        channel should be deleted so outbound dispatching uses the current
        address.  Regression test for BlueBubbles UUID-handle bug.
        """
        user_id = _create_user_with_routes(
            [
                ("bluebubbles", "old-uuid-handle", True),
            ],
            preferred_channel="bluebubbles",
        )

        # Simulate an inbound from the real phone number on the same channel.
        resolved = await _get_or_create_user("bluebubbles", "+15551234567")
        assert resolved.id == user_id

        db = _db_module.SessionLocal()
        try:
            routes = db.query(ChannelRoute).filter_by(user_id=user_id, channel="bluebubbles").all()
            identifiers = {r.channel_identifier for r in routes}
            # The stale UUID route should be gone; only the real number remains.
            assert identifiers == {"+15551234567"}

            user = db.query(User).filter_by(id=user_id).first()
            assert user is not None
            assert user.channel_identifier == "+15551234567"
        finally:
            db.close()

    async def test_stale_route_cleanup_preserves_other_channels(self) -> None:
        """Stale route cleanup must not touch routes for other channels."""
        user_id = _create_user_with_routes(
            [
                ("bluebubbles", "old-uuid-handle", True),
                ("telegram", "tg-12345", True),
            ],
            preferred_channel="telegram",
        )

        await _get_or_create_user("bluebubbles", "+15551234567")

        db = _db_module.SessionLocal()
        try:
            tg_route = db.query(ChannelRoute).filter_by(user_id=user_id, channel="telegram").first()
            assert tg_route is not None
            assert tg_route.channel_identifier == "tg-12345"
        finally:
            db.close()


class TestHeartbeatRouting:
    """Heartbeat uses preferred channel with single fallback."""

    def test_uses_preferred_channel(self) -> None:
        uid = _create_user_with_routes(
            [
                ("telegram", "hb-111", True),
            ],
            preferred_channel="telegram",
        )

        db = _db_module.SessionLocal()
        try:
            user = db.query(User).filter_by(id=uid).first()
            assert user is not None
            result = resolve_heartbeat_route(user, db)
            assert result is not None
            channel_name, route = result
            assert channel_name == "telegram"
            assert route.channel_identifier == "hb-111"
        finally:
            db.close()

    def test_fallback_when_preferred_disabled(self) -> None:
        """Resolve returns an enabled route even if preferred_channel drifted.

        The lookup is pure: it does not rewrite preferred_channel. Write
        paths are responsible for keeping preferred_channel aligned.
        """
        uid = _create_user_with_routes(
            [
                ("telegram", "hb-222", False),
                ("linq", "+15551234567", True),
            ],
            preferred_channel="telegram",
        )

        db = _db_module.SessionLocal()
        try:
            user = db.query(User).filter_by(id=uid).first()
            assert user is not None
            result = resolve_heartbeat_route(user, db)
            assert result is not None
            channel_name, _route = result
            assert channel_name == "linq"

            db.refresh(user)
            assert user.preferred_channel == "telegram"
        finally:
            db.close()

    def test_returns_none_when_all_disabled(self) -> None:
        uid = _create_user_with_routes(
            [
                ("telegram", "hb-333", False),
            ],
            preferred_channel="telegram",
        )

        db = _db_module.SessionLocal()
        try:
            user = db.query(User).filter_by(id=uid).first()
            assert user is not None
            result = resolve_heartbeat_route(user, db)
            assert result is None
        finally:
            db.close()


class TestStartupMigration:
    """_enforce_single_channel fixes users with multiple enabled routes."""

    def test_disables_non_preferred_routes(self) -> None:
        from backend.app.main import _enforce_single_channel

        uid = _create_user_with_routes(
            [
                ("telegram", "mig-111", True),
                ("linq", "+15551234567", True),
                ("bluebubbles", "+15559999999", True),
            ],
            preferred_channel="telegram",
        )

        _enforce_single_channel()

        db = _db_module.SessionLocal()
        try:
            routes = db.query(ChannelRoute).filter_by(user_id=uid).all()
            enabled = [r for r in routes if r.enabled and r.channel != "webchat"]
            assert len(enabled) == 1
            assert enabled[0].channel == "telegram"
        finally:
            db.close()

    def test_preserves_webchat_route(self) -> None:
        from backend.app.main import _enforce_single_channel

        uid = _create_user_with_routes(
            [
                ("telegram", "mig-222", True),
                ("linq", "+15552222222", True),
            ],
            preferred_channel="telegram",
        )

        # Add webchat route separately
        db = _db_module.SessionLocal()
        try:
            db.add(
                ChannelRoute(
                    user_id=uid,
                    channel="webchat",
                    channel_identifier=uid,
                    enabled=True,
                )
            )
            db.commit()
        finally:
            db.close()

        _enforce_single_channel()

        db = _db_module.SessionLocal()
        try:
            webchat = db.query(ChannelRoute).filter_by(user_id=uid, channel="webchat").first()
            assert webchat is not None
            assert webchat.enabled is True
        finally:
            db.close()

    def test_no_change_when_single_channel(self) -> None:
        from backend.app.main import _enforce_single_channel

        uid = _create_user_with_routes(
            [
                ("telegram", "mig-333", True),
            ],
            preferred_channel="telegram",
        )

        _enforce_single_channel()

        db = _db_module.SessionLocal()
        try:
            route = db.query(ChannelRoute).filter_by(user_id=uid, channel="telegram").first()
            assert route is not None
            assert route.enabled is True
        finally:
            db.close()

    def test_realigns_preferred_when_stale(self) -> None:
        """preferred_channel is repointed to an enabled route when stale.

        Multi-channel users whose ``preferred_channel`` points at a channel
        that is not in the enabled set would otherwise be left with every
        messaging route disabled and a dangling preferred_channel.
        """
        from backend.app.main import _enforce_single_channel

        uid = _create_user_with_routes(
            [
                ("linq", "+15551111111", True),
                ("bluebubbles", "+15552222222", True),
            ],
            preferred_channel="telegram",
        )

        _enforce_single_channel()

        db = _db_module.SessionLocal()
        try:
            user = db.query(User).filter_by(id=uid).first()
            assert user is not None
            assert user.preferred_channel in {"linq", "bluebubbles"}
            enabled = (
                db.query(ChannelRoute)
                .filter(
                    ChannelRoute.user_id == uid,
                    ChannelRoute.enabled.is_(True),
                    ChannelRoute.channel != "webchat",
                )
                .all()
            )
            assert len(enabled) == 1
            assert enabled[0].channel == user.preferred_channel
        finally:
            db.close()


class TestPatchDisableSyncsPreferred:
    """Disabling the currently-preferred channel via PATCH updates preferred_channel."""

    def test_disable_preferred_no_other(self, client) -> None:  # noqa: ANN001
        """Disabling the only messaging channel is a no-op for preferred_channel.

        There is no other enabled route to repoint to, and resolve now
        returns None instead of relying on drift-sync.
        """
        db = _db_module.SessionLocal()
        try:
            user = db.query(User).first()
            assert user is not None
            user_id = user.id
            user.preferred_channel = "telegram"
            db.add(
                ChannelRoute(
                    user_id=user_id,
                    channel="telegram",
                    channel_identifier="111",
                    enabled=True,
                )
            )
            db.commit()
        finally:
            db.close()

        resp = client.patch(
            "/api/user/channels/routes/telegram",
            json={"enabled": False},
        )
        assert resp.status_code == 200

        db = _db_module.SessionLocal()
        try:
            user = db.query(User).filter_by(id=user_id).first()
            assert user is not None
            assert user.preferred_channel == "telegram"
        finally:
            db.close()

    def test_disable_preferred_repoints_to_other_enabled(self, client) -> None:  # noqa: ANN001
        """If another enabled route exists, disabling preferred repoints to it.

        This state is unusual under single-channel enforcement, but it can
        arise from legacy data. Fix at the write path so the heartbeat lookup
        never needs to paper over drift.
        """
        db = _db_module.SessionLocal()
        try:
            user = db.query(User).first()
            assert user is not None
            user_id = user.id
            user.preferred_channel = "telegram"
            db.add(
                ChannelRoute(
                    user_id=user_id,
                    channel="telegram",
                    channel_identifier="111",
                    enabled=True,
                )
            )
            db.add(
                ChannelRoute(
                    user_id=user_id,
                    channel="linq",
                    channel_identifier="+15551234567",
                    enabled=True,
                )
            )
            db.commit()
        finally:
            db.close()

        resp = client.patch(
            "/api/user/channels/routes/telegram",
            json={"enabled": False},
        )
        assert resp.status_code == 200

        db = _db_module.SessionLocal()
        try:
            user = db.query(User).filter_by(id=user_id).first()
            assert user is not None
            assert user.preferred_channel == "linq"
        finally:
            db.close()
