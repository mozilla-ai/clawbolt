"""Tests for premium ChannelRoute-based auth in channels.

When ``settings.premium_plugin`` is set, ``is_allowed()`` should approve
senders that have a ``ChannelRoute`` row and reject those that do not,
bypassing the static allowlist entirely.
"""

from unittest.mock import patch

from backend.app.channels.linq import LinqChannel
from backend.app.channels.telegram import TelegramChannel
from backend.app.config import settings
from backend.app.database import SessionLocal
from backend.app.models import ChannelRoute, User

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PATCH_PREMIUM = "premium_plugin"


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


# ---------------------------------------------------------------------------
# BaseChannel._check_premium_route
# ---------------------------------------------------------------------------


def test_check_premium_route_returns_none_in_oss_mode() -> None:
    """_check_premium_route returns None when premium_plugin is not set."""
    channel = TelegramChannel(bot_token="fake")
    with patch.object(settings, _PATCH_PREMIUM, None):
        assert channel._check_premium_route("12345") is None


def test_check_premium_route_returns_true_when_route_exists() -> None:
    """_check_premium_route returns True when a matching ChannelRoute exists."""
    _create_user_with_route("telegram", "12345")
    channel = TelegramChannel(bot_token="fake")
    with patch.object(settings, _PATCH_PREMIUM, "my_plugin"):
        assert channel._check_premium_route("12345") is True


def test_check_premium_route_returns_false_when_no_route() -> None:
    """_check_premium_route returns False when no matching ChannelRoute exists."""
    channel = TelegramChannel(bot_token="fake")
    with patch.object(settings, _PATCH_PREMIUM, "my_plugin"):
        assert channel._check_premium_route("99999") is False


# ---------------------------------------------------------------------------
# TelegramChannel.is_allowed in premium mode
# ---------------------------------------------------------------------------


def test_telegram_premium_allows_routed_sender() -> None:
    """Telegram is_allowed returns True for a sender with a ChannelRoute in premium mode."""
    _create_user_with_route("telegram", "111222333")
    channel = TelegramChannel(bot_token="fake")
    with patch.object(settings, _PATCH_PREMIUM, "my_plugin"):
        assert channel.is_allowed("111222333", "testuser") is True


def test_telegram_premium_rejects_unrouted_sender() -> None:
    """Telegram is_allowed returns False for a sender without a ChannelRoute in premium mode."""
    channel = TelegramChannel(bot_token="fake")
    with patch.object(settings, _PATCH_PREMIUM, "my_plugin"):
        assert channel.is_allowed("999888777", "stranger") is False


def test_telegram_premium_ignores_static_allowlist() -> None:
    """In premium mode, the static allowlist setting is not consulted."""
    channel = TelegramChannel(bot_token="fake")
    with (
        patch.object(settings, _PATCH_PREMIUM, "my_plugin"),
        patch.object(settings, "telegram_allowed_chat_id", "*"),
    ):
        # Even though static allowlist is "*", sender without route is rejected
        assert channel.is_allowed("444555666", "") is False


def test_telegram_oss_falls_through_to_static_allowlist() -> None:
    """In OSS mode (no premium_plugin), the static allowlist is used as before."""
    channel = TelegramChannel(bot_token="fake")
    with (
        patch.object(settings, _PATCH_PREMIUM, None),
        patch.object(settings, "telegram_allowed_chat_id", "12345"),
    ):
        assert channel.is_allowed("12345", "") is True
        assert channel.is_allowed("99999", "") is False


# ---------------------------------------------------------------------------
# LinqChannel.is_allowed in premium mode
# ---------------------------------------------------------------------------


def test_linq_premium_allows_routed_sender() -> None:
    """Linq is_allowed returns True for a sender with a ChannelRoute in premium mode."""
    _create_user_with_route("linq", "+15551234567")
    channel = LinqChannel()
    with patch.object(settings, _PATCH_PREMIUM, "my_plugin"):
        assert channel.is_allowed("+15551234567", "") is True


def test_linq_premium_rejects_unrouted_sender() -> None:
    """Linq is_allowed returns False for a sender without a ChannelRoute in premium mode."""
    channel = LinqChannel()
    with patch.object(settings, _PATCH_PREMIUM, "my_plugin"):
        assert channel.is_allowed("+15559999999", "") is False


def test_linq_premium_ignores_static_allowlist() -> None:
    """In premium mode, the static allowlist setting is not consulted."""
    channel = LinqChannel()
    with (
        patch.object(settings, _PATCH_PREMIUM, "my_plugin"),
        patch.object(settings, "linq_allowed_numbers", "*"),
    ):
        # Even though static allowlist is "*", sender without route is rejected
        assert channel.is_allowed("+15550000000", "") is False


def test_linq_oss_falls_through_to_static_allowlist() -> None:
    """In OSS mode (no premium_plugin), the static allowlist is used as before."""
    channel = LinqChannel()
    with (
        patch.object(settings, _PATCH_PREMIUM, None),
        patch.object(settings, "linq_allowed_numbers", "+15551234567"),
    ):
        assert channel.is_allowed("+15551234567", "") is True
        assert channel.is_allowed("+15559999999", "") is False


# ---------------------------------------------------------------------------
# Cross-channel isolation
# ---------------------------------------------------------------------------


def test_premium_route_scoped_to_channel() -> None:
    """A ChannelRoute for 'telegram' should not authorize the same ID on 'linq'."""
    _create_user_with_route("telegram", "+15551234567")
    linq = LinqChannel()
    with patch.object(settings, _PATCH_PREMIUM, "my_plugin"):
        assert linq.is_allowed("+15551234567", "") is False
