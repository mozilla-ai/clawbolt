"""Tests for realign_preferred_channel invariant helper."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import backend.app.database as _db_module
from backend.app.channel_state import (
    realign_preferred_channel,
    realign_preferred_channel_async,
)
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


# ---------------------------------------------------------------------------
# Async peer (mirrors the sync cases above)
# ---------------------------------------------------------------------------


async def _make_user_async(
    async_db: async_sessionmaker,
    preferred_channel: str = "telegram",
) -> str:
    async with async_db() as db:
        user = User(
            id=str(uuid.uuid4()),
            user_id=f"test-{uuid.uuid4().hex[:8]}",
            channel_identifier="",
            preferred_channel=preferred_channel,
            onboarding_complete=True,
        )
        db.add(user)
        await db.commit()
        return str(user.id)


async def test_async_noop_when_preferred_matches_enabled(
    async_db: async_sessionmaker,
) -> None:
    uid = await _make_user_async(async_db, preferred_channel="telegram")
    async with async_db() as db:
        db.add(
            ChannelRoute(user_id=uid, channel="telegram", channel_identifier="111", enabled=True)
        )
        await db.commit()
        user = (await db.execute(select(User).where(User.id == uid))).scalar_one()
        await realign_preferred_channel_async(db, user)
        await db.commit()
        assert user.preferred_channel == "telegram"


async def test_async_repoints_when_preferred_stale(async_db: async_sessionmaker) -> None:
    uid = await _make_user_async(async_db, preferred_channel="telegram")
    async with async_db() as db:
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
        await db.commit()
        user = (await db.execute(select(User).where(User.id == uid))).scalar_one()
        await realign_preferred_channel_async(db, user)
        await db.commit()
        assert user.preferred_channel == "linq"


async def test_async_flushes_pending_disable(async_db: async_sessionmaker) -> None:
    """async_sessionmaker has autoflush=False (matching SessionLocal). A
    route disabled in-session but not yet committed must still be treated
    as disabled by the helper.
    """
    uid = await _make_user_async(async_db, preferred_channel="telegram")
    async with async_db() as db:
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
        await db.commit()

        telegram_route = (
            await db.execute(
                select(ChannelRoute).where(
                    ChannelRoute.user_id == uid,
                    ChannelRoute.channel == "telegram",
                )
            )
        ).scalar_one()
        telegram_route.enabled = False
        user = (await db.execute(select(User).where(User.id == uid))).scalar_one()
        await realign_preferred_channel_async(db, user)
        await db.commit()
        assert user.preferred_channel == "linq"
