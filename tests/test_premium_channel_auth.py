"""Tests for premium ChannelRoute-based auth in channels.

When an ``is_allowed`` override is registered (e.g. by the premium plugin),
``is_allowed()`` should approve senders that have a ``ChannelRoute`` row and
reject those that do not, bypassing the static allowlist entirely.
"""

from collections.abc import Generator
from unittest.mock import patch

import pytest

from backend.app.channels.base import set_is_allowed_override
from backend.app.channels.linq import LinqChannel
from backend.app.channels.telegram import TelegramChannel
from backend.app.config import settings
from backend.app.database import SessionLocal, db_session
from backend.app.models import ChannelRoute, User

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_user_with_route(channel: str, identifier: str) -> str:
    """Create a User + ChannelRoute in the test DB. Returns user_id."""
    import uuid

    db = SessionLocal()
    try:
        user_id = str(uuid.uuid4())
        user = User(id=user_id, user_id=f"premium-{user_id[:8]}")
        db.add(user)
        db.flush()
        db.add(
            ChannelRoute(
                user_id=user_id,
                channel=channel,
                channel_identifier=identifier,
            )
        )
        db.commit()
        return user_id
    finally:
        db.close()


def _route_based_override(channel_name: str, sender_id: str) -> bool:
    """Test override that checks ChannelRoute, matching premium behavior."""
    with db_session() as db:
        route = (
            db.query(ChannelRoute)
            .filter_by(channel=channel_name, channel_identifier=sender_id)
            .first()
        )
        return route is not None


@pytest.fixture()
def _premium_override() -> Generator[None]:
    """Register the route-based override for the duration of the test."""
    set_is_allowed_override(_route_based_override)
    yield
    set_is_allowed_override(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# BaseChannel._check_premium_route
# ---------------------------------------------------------------------------


def test_check_premium_route_returns_none_without_override() -> None:
    """_check_premium_route returns None when no override is registered."""
    set_is_allowed_override(None)  # type: ignore[arg-type]
    channel = TelegramChannel(bot_token="fake")
    assert channel._check_premium_route("12345") is None


def test_check_premium_route_returns_true_when_route_exists(
    _premium_override: None,
) -> None:
    """_check_premium_route returns True when a matching ChannelRoute exists."""
    _create_user_with_route("telegram", "12345")
    channel = TelegramChannel(bot_token="fake")
    assert channel._check_premium_route("12345") is True


def test_check_premium_route_returns_false_when_no_route(
    _premium_override: None,
) -> None:
    """_check_premium_route returns False when no matching ChannelRoute exists."""
    channel = TelegramChannel(bot_token="fake")
    assert channel._check_premium_route("99999") is False


# ---------------------------------------------------------------------------
# TelegramChannel.is_allowed with override
# ---------------------------------------------------------------------------


def test_telegram_premium_allows_routed_sender(_premium_override: None) -> None:
    """Telegram is_allowed returns True for a sender with a ChannelRoute."""
    _create_user_with_route("telegram", "111222333")
    channel = TelegramChannel(bot_token="fake")
    assert channel.is_allowed("111222333", "testuser") is True


def test_telegram_premium_rejects_unrouted_sender(_premium_override: None) -> None:
    """Telegram is_allowed returns False for a sender without a ChannelRoute."""
    channel = TelegramChannel(bot_token="fake")
    assert channel.is_allowed("999888777", "stranger") is False


def test_telegram_premium_ignores_static_allowlist(_premium_override: None) -> None:
    """With an override registered, the static allowlist setting is not consulted."""
    channel = TelegramChannel(bot_token="fake")
    with patch.object(settings, "telegram_allowed_chat_id", "*"):
        # Even though static allowlist is "*", sender without route is rejected
        assert channel.is_allowed("444555666", "") is False


def test_telegram_oss_falls_through_to_static_allowlist() -> None:
    """Without an override (OSS mode), the static allowlist is used as before."""
    set_is_allowed_override(None)  # type: ignore[arg-type]
    channel = TelegramChannel(bot_token="fake")
    with patch.object(settings, "telegram_allowed_chat_id", "12345"):
        assert channel.is_allowed("12345", "") is True
        assert channel.is_allowed("99999", "") is False


# ---------------------------------------------------------------------------
# LinqChannel.is_allowed with override
# ---------------------------------------------------------------------------


def test_linq_premium_allows_routed_sender(_premium_override: None) -> None:
    """Linq is_allowed returns True for a sender with a ChannelRoute."""
    _create_user_with_route("linq", "+15551234567")
    channel = LinqChannel()
    assert channel.is_allowed("+15551234567", "") is True


def test_linq_premium_rejects_unrouted_sender(_premium_override: None) -> None:
    """Linq is_allowed returns False for a sender without a ChannelRoute."""
    channel = LinqChannel()
    assert channel.is_allowed("+15559999999", "") is False


def test_linq_premium_ignores_static_allowlist(_premium_override: None) -> None:
    """With an override registered, the static allowlist setting is not consulted."""
    channel = LinqChannel()
    with patch.object(settings, "linq_allowed_numbers", "*"):
        # Even though static allowlist is "*", sender without route is rejected
        assert channel.is_allowed("+15550000000", "") is False


def test_linq_oss_falls_through_to_static_allowlist() -> None:
    """Without an override (OSS mode), the static allowlist is used as before."""
    set_is_allowed_override(None)  # type: ignore[arg-type]
    channel = LinqChannel()
    with patch.object(settings, "linq_allowed_numbers", "+15551234567"):
        assert channel.is_allowed("+15551234567", "") is True
        assert channel.is_allowed("+15559999999", "") is False


# ---------------------------------------------------------------------------
# Cross-channel isolation
# ---------------------------------------------------------------------------


def test_premium_route_scoped_to_channel(_premium_override: None) -> None:
    """A ChannelRoute for 'telegram' should not authorize the same ID on 'linq'."""
    _create_user_with_route("telegram", "+15551234567")
    linq = LinqChannel()
    assert linq.is_allowed("+15551234567", "") is False
