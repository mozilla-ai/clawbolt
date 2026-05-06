"""Tests for realign_preferred_channel invariant helper."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.channel_state import realign_preferred_channel_async
from backend.app.database import db_session_async
from backend.app.models import ChannelRoute, User


async def _make_user(preferred_channel: str = "telegram") -> str:
    async with db_session_async() as db:
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


async def test_noop_when_preferred_matches_enabled() -> None:
    uid = await _make_user(preferred_channel="telegram")
    async with db_session_async() as db:
        db.add(
            ChannelRoute(user_id=uid, channel="telegram", channel_identifier="111", enabled=True)
        )
        await db.commit()
        user = (await db.execute(select(User).filter_by(id=uid))).scalar_one()
        await realign_preferred_channel_async(db, user)
        await db.commit()
        assert user.preferred_channel == "telegram"


async def test_noop_when_no_enabled_route_exists() -> None:
    """Nothing to repoint at: preferred_channel is left alone."""
    uid = await _make_user(preferred_channel="telegram")
    async with db_session_async() as db:
        db.add(
            ChannelRoute(user_id=uid, channel="telegram", channel_identifier="111", enabled=False)
        )
        await db.commit()
        user = (await db.execute(select(User).filter_by(id=uid))).scalar_one()
        await realign_preferred_channel_async(db, user)
        await db.commit()
        assert user.preferred_channel == "telegram"


async def test_repoints_when_preferred_stale() -> None:
    uid = await _make_user(preferred_channel="telegram")
    async with db_session_async() as db:
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
        user = (await db.execute(select(User).filter_by(id=uid))).scalar_one()
        await realign_preferred_channel_async(db, user)
        await db.commit()
        assert user.preferred_channel == "linq"


async def test_flushes_pending_disable() -> None:
    """Async session has autoflush=False. A route disabled in-session but not
    yet committed must still be treated as disabled by the helper."""
    uid = await _make_user(preferred_channel="telegram")
    async with db_session_async() as db:
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
            await db.execute(select(ChannelRoute).filter_by(user_id=uid, channel="telegram"))
        ).scalar_one()
        telegram_route.enabled = False
        user = (await db.execute(select(User).filter_by(id=uid))).scalar_one()
        await realign_preferred_channel_async(db, user)
        await db.commit()
        assert user.preferred_channel == "linq"


# ---------------------------------------------------------------------------
# Async peer using the per-test ``async_db`` SAVEPOINT-isolated factory
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
    """async_sessionmaker has autoflush=False. A route disabled in-session
    but not yet committed must still be treated as disabled by the helper.
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
