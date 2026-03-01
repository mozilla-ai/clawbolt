from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    log_level: str = "INFO"
    database_url: str = "postgresql://backshop:backshop@localhost:5432/backshop"
    cors_origins: str = "http://localhost:3000,http://localhost:8000"
    jwt_secret: str = "change-me-in-production"
    jwt_expiry_minutes: int = 15
    premium_plugin: str | None = None

    # Messaging
    messaging_provider: str = "telegram"
    telegram_bot_token: str = ""
    telegram_webhook_secret: str = ""
    telegram_allowed_chat_ids: str = ""  # Comma-separated allowlist; empty = allow all
    telegram_allowed_usernames: str = ""  # Comma-separated @usernames; empty = allow all

    # LLM
    llm_provider: str = "openai"
    llm_model: str = "gpt-4o"
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

    # Whisper
    whisper_model_size: str = "base"

    # Rate limiting
    webhook_rate_limit_max_requests: int = 30
    webhook_rate_limit_window_seconds: int = 60

    # HTTP timeouts
    http_timeout_seconds: float = 30.0
    cloudflared_metrics_timeout_seconds: float = 5.0
    telegram_webhook_timeout_seconds: float = 10.0

    # Heartbeat
    heartbeat_enabled: bool = True
    heartbeat_interval_minutes: int = 30
    heartbeat_max_daily_messages: int = 5
    heartbeat_quiet_hours_start: int = 20  # 8 PM — fallback when no business_hours
    heartbeat_quiet_hours_end: int = 7  # 7 AM
    heartbeat_model: str = ""  # empty = fall back to llm_model
    heartbeat_provider: str = ""  # empty = fall back to llm_provider

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()

TELEGRAM_API_BASE = "https://api.telegram.org"
