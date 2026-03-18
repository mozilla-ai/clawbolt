"""Verify that model and function defaults reference Settings, not hardcoded values."""

from backend.app.config import settings
from backend.app.models import User


def test_user_data_preferred_channel_from_settings() -> None:
    """User.preferred_channel should default to settings.messaging_provider."""
    user = User()
    assert user.preferred_channel == settings.messaging_provider


def test_user_data_heartbeat_frequency_from_settings() -> None:
    """User.heartbeat_frequency should default to settings.heartbeat_default_frequency."""
    user = User()
    assert user.heartbeat_frequency == settings.heartbeat_default_frequency
