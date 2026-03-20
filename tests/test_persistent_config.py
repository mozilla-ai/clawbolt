"""Tests for persistent config.json loading and saving."""

import json
import os
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from backend.app.config import (
    PERSISTABLE_SETTINGS,
    load_persistent_config,
    save_persistent_config,
    settings,
    update_settings,
)


@pytest.fixture()
def config_dir(tmp_path: Path) -> Path:
    """Return a temporary directory for config.json."""
    return tmp_path


@pytest.fixture()
def config_path(config_dir: Path) -> Path:
    """Return the path to a temporary config.json."""
    return config_dir / "config.json"


@pytest.fixture(autouse=True)
def _reset_settings() -> Iterator[None]:
    """Save and restore telegram-related settings around each test.

    Also temporarily remove the corresponding env vars so that earlier tests
    (which may have called ``load_dotenv()``) do not pollute ``os.environ``
    and cause ``load_persistent_config`` to skip config.json values.
    """
    originals = {key: getattr(settings, key) for key in PERSISTABLE_SETTINGS}
    env_keys = [key.upper() for key in PERSISTABLE_SETTINGS]
    saved_env = {k: os.environ.pop(k) for k in env_keys if k in os.environ}
    yield
    for key, value in originals.items():
        setattr(settings, key, value)
    # Restore any env vars we removed.
    for k, v in saved_env.items():
        os.environ[k] = v


def test_load_nonexistent_config_returns_empty(config_path: Path) -> None:
    """Loading a missing config.json returns an empty dict and is a no-op."""
    result = load_persistent_config(path=config_path)
    assert result == {}


def test_load_config_applies_values(config_path: Path) -> None:
    """Values from config.json are applied to the settings singleton."""
    config_path.write_text(
        json.dumps(
            {
                "telegram_bot_token": "saved-token",
                "telegram_allowed_chat_ids": "111,222",
            }
        )
    )

    result = load_persistent_config(path=config_path)

    assert result["telegram_bot_token"] == "saved-token"
    assert settings.telegram_bot_token == "saved-token"
    assert settings.telegram_allowed_chat_ids == "111,222"


def test_load_config_env_var_takes_precedence(config_path: Path) -> None:
    """Environment variables override config.json values."""
    config_path.write_text(json.dumps({"telegram_bot_token": "from-config-json"}))

    with patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "from-env-var"}):
        load_persistent_config(path=config_path)

    # The env var should win, so settings should NOT be overwritten.
    assert settings.telegram_bot_token != "from-config-json"


def test_load_config_ignores_non_persistable_keys(config_path: Path) -> None:
    """Keys not in PERSISTABLE_SETTINGS are ignored."""
    original_log_level = settings.log_level
    config_path.write_text(json.dumps({"log_level": "DEBUG", "telegram_bot_token": "tok"}))

    load_persistent_config(path=config_path)

    assert settings.log_level == original_log_level
    assert settings.telegram_bot_token == "tok"


def test_load_config_handles_corrupt_json(config_path: Path) -> None:
    """Corrupt JSON is handled gracefully, returning an empty dict."""
    config_path.write_text("{invalid json!!!")

    result = load_persistent_config(path=config_path)

    assert result == {}


def test_save_creates_file(config_path: Path) -> None:
    """save_persistent_config creates config.json if it does not exist."""
    assert not config_path.exists()

    save_persistent_config({"telegram_bot_token": "new-tok"}, path=config_path)

    assert config_path.is_file()
    data = json.loads(config_path.read_text())
    assert data["telegram_bot_token"] == "new-tok"


def test_save_merges_with_existing(config_path: Path) -> None:
    """save_persistent_config merges new keys into existing config."""
    config_path.write_text(json.dumps({"telegram_bot_token": "existing-tok"}))

    save_persistent_config({"telegram_allowed_chat_ids": "111"}, path=config_path)

    data = json.loads(config_path.read_text())
    assert data["telegram_bot_token"] == "existing-tok"
    assert data["telegram_allowed_chat_ids"] == "111"


def test_save_overwrites_existing_key(config_path: Path) -> None:
    """save_persistent_config overwrites a key that already exists."""
    config_path.write_text(json.dumps({"telegram_bot_token": "old-tok"}))

    save_persistent_config({"telegram_bot_token": "new-tok"}, path=config_path)

    data = json.loads(config_path.read_text())
    assert data["telegram_bot_token"] == "new-tok"


def test_save_creates_parent_directories(tmp_path: Path) -> None:
    """save_persistent_config creates parent directories if needed."""
    deep_path = tmp_path / "nested" / "dir" / "config.json"

    save_persistent_config({"telegram_bot_token": "tok"}, path=deep_path)

    assert deep_path.is_file()
    data = json.loads(deep_path.read_text())
    assert data["telegram_bot_token"] == "tok"


def test_save_handles_corrupt_existing_file(config_path: Path) -> None:
    """save_persistent_config handles a corrupt existing file by overwriting it."""
    config_path.write_text("{corrupt!!!")

    save_persistent_config({"telegram_bot_token": "fresh"}, path=config_path)

    data = json.loads(config_path.read_text())
    assert data["telegram_bot_token"] == "fresh"


def test_load_all_persistable_settings(config_path: Path) -> None:
    """All four persistable settings can be loaded from config.json."""
    all_values = {
        "telegram_bot_token": "bot-token-value",
        "telegram_allowed_chat_ids": "111,222",
        "telegram_webhook_secret": "secret-value",
    }
    config_path.write_text(json.dumps(all_values))

    load_persistent_config(path=config_path)

    for key, expected in all_values.items():
        assert getattr(settings, key) == expected, f"{key} was not applied"


def test_round_trip_save_then_load(config_path: Path) -> None:
    """Values saved with save_persistent_config can be loaded back."""
    save_persistent_config(
        {"telegram_bot_token": "rt-token", "telegram_allowed_chat_ids": "111"},
        path=config_path,
    )

    # Reset settings to defaults
    settings.telegram_bot_token = ""
    settings.telegram_allowed_chat_ids = ""

    load_persistent_config(path=config_path)

    assert settings.telegram_bot_token == "rt-token"
    assert settings.telegram_allowed_chat_ids == "111"


# ---------------------------------------------------------------------------
# update_settings() validation
# ---------------------------------------------------------------------------


def test_update_settings_applies_valid_values() -> None:
    """update_settings applies valid string values to the singleton."""
    update_settings({"telegram_bot_token": "validated-tok"})
    assert settings.telegram_bot_token == "validated-tok"


def test_update_settings_rejects_non_persistable_key() -> None:
    """update_settings raises ValueError for keys not in PERSISTABLE_SETTINGS."""
    with pytest.raises(ValueError, match="not a persistable setting"):
        update_settings({"log_level": "DEBUG"})


def test_update_settings_rejects_unknown_key() -> None:
    """update_settings raises ValueError for keys that don't exist on Settings."""
    with pytest.raises(ValueError, match="not a persistable setting"):
        update_settings({"totally_unknown_field": "value"})


def test_update_settings_rejects_wrong_type() -> None:
    """update_settings raises ValueError when the value fails Pydantic validation."""
    with pytest.raises(ValueError):
        update_settings({"telegram_bot_token": 12345})


def test_update_settings_multiple_keys() -> None:
    """update_settings can apply multiple valid keys at once."""
    update_settings(
        {
            "telegram_bot_token": "multi-tok",
            "telegram_allowed_chat_ids": "111,222",
        }
    )
    assert settings.telegram_bot_token == "multi-tok"
    assert settings.telegram_allowed_chat_ids == "111,222"
