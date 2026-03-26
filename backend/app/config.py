import contextlib
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
from typing import Any

from pydantic import Field, SecretStr, ValidationError
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
    database_url: str = "postgresql://clawbolt:clawbolt@localhost:5432/clawbolt"
    cors_origins: str = "http://localhost:3000,http://localhost:8000"
    jwt_secret: str = "change-me-in-production"
    jwt_expiry_minutes: int = Field(default=15, ge=1)
    premium_plugin: str | None = None

    # Messaging
    messaging_provider: str = "telegram"
    telegram_bot_token: str = ""
    telegram_webhook_secret: str = ""
    telegram_allowed_chat_id: str = ""  # Single numeric chat ID, or "*" for all; empty = deny all

    # LLM
    llm_provider: str = ""
    llm_model: str = ""
    llm_api_base: str | None = None
    vision_model: str = ""  # empty = fall back to llm_model
    vision_provider: str = ""  # empty = fall back to llm_provider
    reasoning_effort: str = "auto"  # none, minimal, low, medium, high, xhigh, auto
    llm_max_tokens_agent: int = Field(default=1024, ge=1)
    llm_max_tokens_heartbeat: int = Field(default=300, ge=1)
    llm_max_tokens_vision: int = Field(default=1000, ge=1)

    # Storage
    storage_provider: str = "local"  # "local", "dropbox", or "google_drive"
    dropbox_access_token: str = ""
    google_drive_credentials_json: str = ""
    file_storage_base_dir: str = "data/storage"

    # Whisper
    whisper_model_size: str = "base"
    whisper_device: str = "cpu"
    whisper_compute_type: str = "int8"

    # Agent loop
    approval_timeout_seconds: int = Field(default=120, ge=1)
    message_batch_window_ms: int = Field(default=1500, ge=100)
    max_tool_rounds: int = Field(default=10, ge=1)
    max_input_tokens: int = Field(default=120_000, ge=1)
    context_trim_target_tokens: int = Field(default=80_000, ge=1)
    llm_max_retries: int = Field(default=3, ge=1)

    # Conversation & memory
    conversation_history_limit: int = Field(default=20, ge=1)
    memory_recall_limit: int = Field(default=20, ge=1)
    compaction_enabled: bool = True
    compaction_model: str = ""  # empty = fall back to llm_model
    compaction_provider: str = ""  # empty = fall back to llm_provider
    compaction_max_tokens: int = Field(default=1500, ge=1)

    # Rate limiting
    webhook_rate_limit_max_requests: int = Field(default=30, ge=1)
    webhook_rate_limit_window_seconds: int = Field(default=60, ge=1)
    rate_limit_trust_proxy: bool = False

    # Media
    max_media_size_bytes: int = Field(default=20_971_520, ge=1)  # 20 MB

    # QuickBooks Online
    quickbooks_client_id: str = ""
    quickbooks_client_secret: str = ""
    quickbooks_environment: str = "sandbox"  # "sandbox" or "production"

    # Linq (iMessage/RCS/SMS)
    linq_api_token: str = ""
    linq_from_number: str = ""  # E.164 format
    linq_webhook_signing_secret: str = ""
    linq_allowed_numbers: str = ""  # E.164 phone number, "*", or empty
    linq_preferred_service: str = "iMessage"  # "iMessage", "SMS", or "RCS"

    # Google Calendar
    google_calendar_client_id: str = ""
    google_calendar_client_secret: str = ""

    # OAuth
    app_base_url: str = "http://localhost:8000"  # Public URL for OAuth callbacks

    # Encryption (used for OAuth tokens at rest; generate with: python -c "import secrets; print(secrets.token_urlsafe(32))")
    encryption_key: SecretStr = SecretStr("")

    # HTTP timeouts
    http_timeout_seconds: float = Field(default=30.0, gt=0)
    cloudflared_metrics_timeout_seconds: float = Field(default=5.0, gt=0)
    telegram_webhook_timeout_seconds: float = Field(default=10.0, gt=0)

    # Heartbeat
    heartbeat_enabled: bool = True
    heartbeat_default_frequency: str = "30m"
    heartbeat_interval_minutes: int = Field(default=30, ge=1)
    heartbeat_max_daily_messages: int = Field(default=5, ge=1)
    heartbeat_quiet_hours_start: int = Field(default=20, ge=0, le=23)  # 8 PM
    heartbeat_quiet_hours_end: int = Field(default=7, ge=0, le=23)  # 7 AM
    heartbeat_model: str = ""  # empty = fall back to llm_model
    heartbeat_provider: str = ""  # empty = fall back to llm_provider
    heartbeat_concurrency: int = Field(default=5, ge=1)
    heartbeat_recent_messages_count: int = Field(default=5, ge=1)

    # Observability
    log_request_timing: bool = False  # Set True (or LOG_REQUEST_TIMING=1) to log per-request timing

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
        "telegram_allowed_chat_id",
        "telegram_webhook_secret",
        "linq_api_token",
        "linq_from_number",
        "linq_webhook_signing_secret",
        "linq_allowed_numbers",
        "linq_preferred_service",
        "llm_provider",
        "llm_model",
        "llm_api_base",
        "vision_model",
        "vision_provider",
        "heartbeat_model",
        "heartbeat_provider",
        "compaction_model",
        "compaction_provider",
        "reasoning_effort",
        "storage_provider",
        "dropbox_access_token",
        "google_drive_credentials_json",
        "file_storage_base_dir",
    }
)


def update_settings(updates: dict[str, Any]) -> None:
    """Validate and apply runtime updates to the settings singleton.

    Only keys listed in ``PERSISTABLE_SETTINGS`` are accepted.  Each value is
    validated against the Pydantic field definition before being applied, so
    type mismatches raise ``ValueError``.

    Validation runs for all keys before any are applied, so a failure on one
    key never leaves the singleton in a partially-updated state.
    """
    for key, value in updates.items():
        if key not in PERSISTABLE_SETTINGS:
            raise ValueError(
                f"{key!r} is not a persistable setting (allowed: {sorted(PERSISTABLE_SETTINGS)})"
            )
        try:
            Settings.model_validate({key: value})
        except ValidationError as exc:
            raise ValueError(str(exc)) from exc

    for key, value in updates.items():
        setattr(settings, key, value)


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

    filtered: dict[str, Any] = {}
    for key, value in data.items():
        if key not in PERSISTABLE_SETTINGS:
            continue
        # Environment variables always win.
        env_name = key.upper()
        if os.environ.get(env_name):
            continue
        filtered[key] = value

    if filtered:
        update_settings(filtered)

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


def log_config_warnings(s: Settings | None = None) -> list[str]:
    """Log warnings for unusual but valid config values. Returns the warnings."""
    s = s or settings
    warnings: list[str] = []

    if s.max_tool_rounds > 50:
        warnings.append(f"max_tool_rounds={s.max_tool_rounds} is unusually high (default: 10)")
    if s.message_batch_window_ms > 10_000:
        warnings.append(
            f"message_batch_window_ms={s.message_batch_window_ms} is unusually high (default: 1500)"
        )
    if s.llm_max_tokens_agent < 100:
        warnings.append(
            f"llm_max_tokens_agent={s.llm_max_tokens_agent} is very low"
            " and may produce truncated responses"
        )
    if s.context_trim_target_tokens >= s.max_input_tokens:
        warnings.append(
            f"context_trim_target_tokens ({s.context_trim_target_tokens})"
            f" >= max_input_tokens ({s.max_input_tokens});"
            " trimming will never trigger"
        )

    enc_key = s.encryption_key.get_secret_value()
    if not enc_key:
        warnings.append(
            "encryption_key is not set; OAuth tokens will be stored unencrypted."
            " Set ENCRYPTION_KEY to a random value"
            ' (python -c "import secrets; print(secrets.token_urlsafe(32))")'
        )
    elif len(enc_key) < 16:
        warnings.append(
            f"encryption_key is only {len(enc_key)} characters;"
            " use at least 32 characters of random data for production"
        )

    for w in warnings:
        logger.warning("Config: %s", w)

    return warnings
