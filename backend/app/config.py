import hashlib
import hmac

from pydantic_settings import BaseSettings


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
    contractor_data_dir: str = "data/contractors"
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
    message_batch_window_ms: int = 1500  # Batch rapid-fire messages per contractor
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
    heartbeat_idle_days: int = 3  # flag contractors with no inbound messages for N days
    heartbeat_model: str = ""  # empty = fall back to llm_model
    heartbeat_provider: str = ""  # empty = fall back to llm_provider
    heartbeat_concurrency: int = 5  # max concurrent contractor evaluations per tick
    checklist_daily_interval_hours: int = 20
    heartbeat_recent_messages_count: int = 5

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()

TELEGRAM_API_BASE = "https://api.telegram.org"
