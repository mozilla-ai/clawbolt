"""Tests for realign_preferred_channel invariant helper."""

import uuid

import backend.app.database as _db_module
from backend.app.channel_state import realign_preferred_channel
from backend.app.models import ChannelRoute, User


def _make_user(preferred_channel: str = "telegram") -> str:
    db = _db_module.SessionLocal()
    try:
        user = User(
            id=str(uuid.uuid4()),
            user_id=f"test-{uuid.uuid4().hex[:8]}",
            channel_identifier="",
            preferred_channel=preferred_channel,
            onboarding_complete=True,
        )
        db.add(user)
        db.commit()
        uid = user.id
    finally:
        db.close()
    return uid


def test_noop_when_preferred_matches_enabled() -> None:
    uid = _make_user(preferred_channel="telegram")
    db = _db_module.SessionLocal()
    try:
        db.add(
            ChannelRoute(user_id=uid, channel="telegram", channel_identifier="111", enabled=True)
        )
        db.commit()
        user = db.query(User).filter_by(id=uid).first()
        assert user is not None
        realign_preferred_channel(db, user)
        db.commit()
        assert user.preferred_channel == "telegram"
    finally:
        db.close()


def test_noop_when_no_enabled_route_exists() -> None:
    """Nothing to repoint at: preferred_channel is left alone."""
    uid = _make_user(preferred_channel="telegram")
    db = _db_module.SessionLocal()
    try:
        db.add(
            ChannelRoute(user_id=uid, channel="telegram", channel_identifier="111", enabled=False)
        )
        db.commit()
        user = db.query(User).filter_by(id=uid).first()
        assert user is not None
        realign_preferred_channel(db, user)
        db.commit()
        assert user.preferred_channel == "telegram"
    finally:
        db.close()


def test_repoints_when_preferred_stale() -> None:
    uid = _make_user(preferred_channel="telegram")
    db = _db_module.SessionLocal()
    try:
        db.add(
            ChannelRoute(user_id=uid, channel="telegram", channel_identifier="111", enabled=False)
        )
        db.add(
            ChannelRoute(
                user_id=uid,
                channel="linq",
                channel_identifier="+15551234567",
                enabled=True,
            )
        )
        db.commit()
        user = db.query(User).filter_by(id=uid).first()
        assert user is not None
        realign_preferred_channel(db, user)
        db.commit()
        assert user.preferred_channel == "linq"
    finally:
        db.close()


def test_flushes_pending_disable() -> None:
    """SessionLocal has autoflush=False. A route disabled in-session but not
    yet committed must still be treated as disabled by the helper."""
    uid = _make_user(preferred_channel="telegram")
    db = _db_module.SessionLocal()
    try:
        db.add(
            ChannelRoute(user_id=uid, channel="telegram", channel_identifier="111", enabled=True)
        )
        db.add(
            ChannelRoute(
                user_id=uid,
                channel="linq",
                channel_identifier="+15551234567",
                enabled=True,
            )
        )
        db.commit()

        telegram_route = db.query(ChannelRoute).filter_by(user_id=uid, channel="telegram").first()
        assert telegram_route is not None
        telegram_route.enabled = False
        user = db.query(User).filter_by(id=uid).first()
        assert user is not None
        realign_preferred_channel(db, user)
        db.commit()
        assert user.preferred_channel == "linq"
    finally:
        db.close()
