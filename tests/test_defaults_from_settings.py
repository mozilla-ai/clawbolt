"""Verify that model and function defaults reference Settings, not hardcoded values."""

from backend.app.agent.file_store import UserData, make_client_slug
from backend.app.config import settings


def test_user_data_preferred_channel_from_settings() -> None:
    """UserData.preferred_channel should default to settings.messaging_provider."""
    user = UserData()
    assert user.preferred_channel == settings.messaging_provider


def test_user_data_heartbeat_frequency_from_settings() -> None:
    """UserData.heartbeat_frequency should default to settings.heartbeat_default_frequency."""
    user = UserData()
    assert user.heartbeat_frequency == settings.heartbeat_default_frequency


def test_user_data_folder_scheme_from_settings() -> None:
    """UserData.folder_scheme should default to settings.default_folder_scheme."""
    user = UserData()
    assert user.folder_scheme == settings.default_folder_scheme


def test_make_client_slug_uses_settings_folder_scheme() -> None:
    """make_client_slug should use settings.default_folder_scheme when not passed."""
    slug = make_client_slug(name="John Doe")
    expected = make_client_slug(name="John Doe", folder_scheme=settings.default_folder_scheme)
    assert slug == expected
