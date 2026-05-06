"""Tests for premium ChannelRoute-based auth in channels.

When an ``is_allowed`` override is registered (e.g. by the premium plugin),
``is_allowed()`` should approve senders that have a ``ChannelRoute`` row and
reject those that do not, bypassing the static allowlist entirely.
"""

from collections.abc import AsyncGenerator
from unittest.mock import patch

import pytest_asyncio
from sqlalchemy import select

from backend.app.channels.base import set_is_allowed_override
from backend.app.channels.linq import LinqChannel
from backend.app.channels.telegram import TelegramChannel
from backend.app.config import settings
from backend.app.database import db_session_async
from backend.app.models import ChannelRoute, User

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_user_with_route(channel: str, identifier: str) -> str:
    """Create a User + ChannelRoute in the test DB. Returns user_id."""
    import uuid

    user_id = str(uuid.uuid4())
    async with db_session_async() as db:
        user = User(id=user_id, user_id=f"premium-{user_id[:8]}")
        db.add(user)
        await db.flush()
        db.add(
            ChannelRoute(
                user_id=user_id,
                channel=channel,
                channel_identifier=identifier,
            )
        )
        await db.commit()
    return user_id


async def _route_based_override(channel_name: str, sender_id: str) -> bool:
    """Test override that checks ChannelRoute, matching premium behavior."""
    async with db_session_async() as db:
        route = (
            await db.execute(
                select(ChannelRoute).filter_by(channel=channel_name, channel_identifier=sender_id)
            )
        ).scalar_one_or_none()
        return route is not None


@pytest_asyncio.fixture()
async def _premium_override() -> AsyncGenerator[None]:
    """Register the route-based override for the duration of the test."""
    set_is_allowed_override(_route_based_override)
    yield
    set_is_allowed_override(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# BaseChannel._check_premium_route
# ---------------------------------------------------------------------------


async def test_check_premium_route_returns_none_without_override() -> None:
    """_check_premium_route returns None when no override is registered."""
    set_is_allowed_override(None)  # type: ignore[arg-type]
    channel = TelegramChannel(bot_token="fake")
    assert await channel._check_premium_route("12345") is None


async def test_check_premium_route_returns_true_when_route_exists(
    _premium_override: None,
) -> None:
    """_check_premium_route returns True when a matching ChannelRoute exists."""
    await _create_user_with_route("telegram", "12345")
    channel = TelegramChannel(bot_token="fake")
    assert await channel._check_premium_route("12345") is True


async def test_check_premium_route_returns_false_when_no_route(
    _premium_override: None,
) -> None:
    """_check_premium_route returns False when no matching ChannelRoute exists."""
    channel = TelegramChannel(bot_token="fake")
    assert await channel._check_premium_route("99999") is False


# ---------------------------------------------------------------------------
# TelegramChannel.is_allowed with override
# ---------------------------------------------------------------------------


async def test_telegram_premium_allows_routed_sender(_premium_override: None) -> None:
    """Telegram is_allowed returns True for a sender with a ChannelRoute."""
    await _create_user_with_route("telegram", "111222333")
    channel = TelegramChannel(bot_token="fake")
    assert await channel.is_allowed("111222333", "testuser") is True


async def test_telegram_premium_rejects_unrouted_sender(_premium_override: None) -> None:
    """Telegram is_allowed returns False for a sender without a ChannelRoute."""
    channel = TelegramChannel(bot_token="fake")
    assert await channel.is_allowed("999888777", "stranger") is False


async def test_telegram_premium_ignores_static_allowlist(_premium_override: None) -> None:
    """With an override registered, the static allowlist setting is not consulted."""
    channel = TelegramChannel(bot_token="fake")
    with patch.object(settings, "telegram_allowed_chat_id", "*"):
        # Even though static allowlist is "*", sender without route is rejected
        assert await channel.is_allowed("444555666", "") is False


async def test_telegram_oss_falls_through_to_static_allowlist() -> None:
    """Without an override (OSS mode), the static allowlist is used as before."""
    set_is_allowed_override(None)  # type: ignore[arg-type]
    channel = TelegramChannel(bot_token="fake")
    with patch.object(settings, "telegram_allowed_chat_id", "12345"):
        assert await channel.is_allowed("12345", "") is True
        assert await channel.is_allowed("99999", "") is False


# ---------------------------------------------------------------------------
# LinqChannel.is_allowed with override
# ---------------------------------------------------------------------------


async def test_linq_premium_allows_routed_sender(_premium_override: None) -> None:
    """Linq is_allowed returns True for a sender with a ChannelRoute."""
    await _create_user_with_route("linq", "+15551234567")
    channel = LinqChannel()
    assert await channel.is_allowed("+15551234567", "") is True


async def test_linq_premium_rejects_unrouted_sender(_premium_override: None) -> None:
    """Linq is_allowed returns False for a sender without a ChannelRoute."""
    channel = LinqChannel()
    assert await channel.is_allowed("+15559999999", "") is False


async def test_linq_premium_ignores_static_allowlist(_premium_override: None) -> None:
    """With an override registered, the static allowlist setting is not consulted."""
    channel = LinqChannel()
    with patch.object(settings, "linq_allowed_numbers", "*"):
        # Even though static allowlist is "*", sender without route is rejected
        assert await channel.is_allowed("+15550000000", "") is False


async def test_linq_oss_falls_through_to_static_allowlist() -> None:
    """Without an override (OSS mode), the static allowlist is used as before."""
    set_is_allowed_override(None)  # type: ignore[arg-type]
    channel = LinqChannel()
    with patch.object(settings, "linq_allowed_numbers", "+15551234567"):
        assert await channel.is_allowed("+15551234567", "") is True
        assert await channel.is_allowed("+15559999999", "") is False


# ---------------------------------------------------------------------------
# Cross-channel isolation
# ---------------------------------------------------------------------------


async def test_premium_route_scoped_to_channel(_premium_override: None) -> None:
    """A ChannelRoute for 'telegram' should not authorize the same ID on 'linq'."""
    await _create_user_with_route("telegram", "+15551234567")
    linq = LinqChannel()
    assert await linq.is_allowed("+15551234567", "") is False
