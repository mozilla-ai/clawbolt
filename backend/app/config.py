import contextlib
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
from typing import Any

from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)


def _derive_webhook_secret(bot_token: str) -> str:
    """Derive a deterministic webhook secret from the bot token via HMAC-SHA256."""
    return hmac.new(
        key=b"backshop-telegram-webhook-secret",
        msg=bot_token.encode(),
        digestmod=hashlib.sha256,
    ).hexdigest()


def get_effective_webhook_secret(s: "Settings") -> str:
    """Return the explicit secret if set, otherwise derive one from the bot token."""
    if s.telegram_webhook_secret:
        return s.telegram_webhook_secret
    if s.telegram_bot_token:
        return _derive_webhook_secret(s.telegram_bot_token)
    return ""


class Settings(BaseSettings):
    log_level: str = "INFO"
    data_dir: str = "data/users"
    cors_origins: str = "http://localhost:3000,http://localhost:8000"
    jwt_secret: str = "change-me-in-production"
    jwt_expiry_minutes: int = 15
    premium_plugin: str | None = None

    # Messaging
    messaging_provider: str = "telegram"
    telegram_bot_token: str = ""
    telegram_webhook_secret: str = ""
    telegram_allowed_chat_ids: str = (
        ""  # Comma-separated allowlist, or "*" for all; empty = deny all
    )
    telegram_allowed_usernames: str = (
        ""  # Comma-separated @usernames, or "*" for all; empty = deny all
    )

    # LLM
    llm_provider: str = ""
    llm_model: str = ""
    llm_api_base: str | None = None
    vision_model: str = ""  # empty = fall back to llm_model
    llm_max_tokens_agent: int = 500
    llm_max_tokens_heartbeat: int = 300
    llm_max_tokens_vision: int = 1000

    # Storage
    storage_provider: str = "local"  # "local", "dropbox", or "google_drive"
    dropbox_access_token: str = ""
    google_drive_credentials_json: str = ""
    pdf_storage_dir: str = "data/estimates"
    file_storage_base_dir: str = "data/storage"

    # Whisper
    whisper_model_size: str = "base"
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"

    # Agent loop
    approval_timeout_seconds: int = 120
    message_batch_window_ms: int = 1500  # Batch rapid-fire messages per user
    max_tool_rounds: int = 10
    max_input_tokens: int = 120_000
    context_trim_target_tokens: int = 80_000
    rate_limit_retry_delay: float = 2.0

    # Conversation & memory
    conversation_timeout_hours: int = 4
    conversation_history_limit: int = 20
    memory_recall_limit: int = 20
    compaction_enabled: bool = True
    compaction_model: str = ""  # empty = fall back to llm_model
    compaction_provider: str = ""  # empty = fall back to llm_provider
    compaction_max_tokens: int = 500
    heartbeat_stale_estimate_hours: int = 24

    # Rate limiting
    webhook_rate_limit_max_requests: int = 30
    webhook_rate_limit_window_seconds: int = 60
    rate_limit_trust_proxy: bool = False

    # Media
    max_media_size_bytes: int = 20_971_520  # 20 MB

    # HTTP timeouts
    http_timeout_seconds: float = 30.0
    cloudflared_metrics_timeout_seconds: float = 5.0
    telegram_webhook_timeout_seconds: float = 10.0

    # Heartbeat
    heartbeat_enabled: bool = True
    heartbeat_interval_minutes: int = 30
    heartbeat_max_daily_messages: int = 5
    heartbeat_quiet_hours_start: int = 20  # 8 PM
    heartbeat_quiet_hours_end: int = 7  # 7 AM
    heartbeat_idle_days: int = 3  # flag users with no inbound messages for N days
    heartbeat_model: str = ""  # empty = fall back to llm_model
    heartbeat_provider: str = ""  # empty = fall back to llm_provider
    heartbeat_concurrency: int = 5  # max concurrent user evaluations per tick
    heartbeat_recent_messages_count: int = 5

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()

TELEGRAM_API_BASE = "https://api.telegram.org"

# ---------------------------------------------------------------------------
# Persistent config.json -- survives container restarts via volume mount
# ---------------------------------------------------------------------------

# Settings that can be persisted to config.json at runtime.
PERSISTABLE_SETTINGS: frozenset[str] = frozenset(
    {
        "telegram_bot_token",
        "telegram_allowed_chat_ids",
        "telegram_allowed_usernames",
        "telegram_webhook_secret",
    }
)


def _config_json_path() -> Path:
    """Return the path to config.json inside the volume-mounted data directory.

    ``data_dir`` typically points at ``data/users``; the config file lives one
    level up so it sits directly inside the mounted ``data/`` volume.
    """
    return Path(settings.data_dir).parent / "config.json"


def load_persistent_config(path: Path | None = None) -> dict[str, Any]:
    """Load config.json and apply values to the settings singleton.

    Values from config.json override defaults but are themselves overridden by
    real environment variables.  Returns the loaded dict (empty if the file
    does not exist).
    """
    config_path = path or _config_json_path()
    if not config_path.is_file():
        return {}

    try:
        data: dict[str, Any] = json.loads(config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read %s: %s", config_path, exc)
        return {}

    for key, value in data.items():
        if key not in PERSISTABLE_SETTINGS:
            continue
        # Environment variables always win.
        env_name = key.upper()
        if os.environ.get(env_name):
            continue
        setattr(settings, key, value)

    return data


def save_persistent_config(updates: dict[str, str], path: Path | None = None) -> None:
    """Merge *updates* into config.json, creating the file if needed."""
    config_path = path or _config_json_path()

    existing: dict[str, Any] = {}
    if config_path.is_file():
        with contextlib.suppress(json.JSONDecodeError, OSError):
            existing = json.loads(config_path.read_text(encoding="utf-8"))

    existing.update(updates)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(existing, indent=2) + "\n", encoding="utf-8")
