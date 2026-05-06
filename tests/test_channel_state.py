"""Tests for realign_preferred_channel invariant helper."""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.channel_state import realign_preferred_channel
from backend.app.models import ChannelRoute, User


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


@pytest.mark.asyncio()
async def test_noop_when_preferred_matches_enabled(
    async_db: async_sessionmaker,
) -> None:
    uid = await _make_user_async(async_db, preferred_channel="telegram")
    async with async_db() as db:
        db.add(
            ChannelRoute(user_id=uid, channel="telegram", channel_identifier="111", enabled=True)
        )
        await db.commit()
        user = (await db.execute(select(User).where(User.id == uid))).scalar_one()
        await realign_preferred_channel(db, user)
        await db.commit()
        assert user.preferred_channel == "telegram"


@pytest.mark.asyncio()
async def test_noop_when_no_enabled_route_exists(async_db: async_sessionmaker) -> None:
    """Nothing to repoint at: preferred_channel is left alone."""
    uid = await _make_user_async(async_db, preferred_channel="telegram")
    async with async_db() as db:
        db.add(
            ChannelRoute(user_id=uid, channel="telegram", channel_identifier="111", enabled=False)
        )
        await db.commit()
        user = (await db.execute(select(User).where(User.id == uid))).scalar_one()
        await realign_preferred_channel(db, user)
        await db.commit()
        assert user.preferred_channel == "telegram"


@pytest.mark.asyncio()
async def test_repoints_when_preferred_stale(async_db: async_sessionmaker) -> None:
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
        await realign_preferred_channel(db, user)
        await db.commit()
        assert user.preferred_channel == "linq"


@pytest.mark.asyncio()
async def test_flushes_pending_disable(async_db: async_sessionmaker) -> None:
    """async_sessionmaker has autoflush=False. Pending route changes must flush."""
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
        await realign_preferred_channel(db, user)
        await db.commit()
        assert user.preferred_channel == "linq"
