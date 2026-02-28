from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql://backshop:backshop@localhost:5432/backshop"
    cors_origins: str = "*"
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
    vision_model: str = "gpt-4o"

    # Storage
    storage_provider: str = "dropbox"  # "dropbox" or "google_drive"
    dropbox_access_token: str = ""
    google_drive_credentials_json: str = ""

    # Whisper
    whisper_model_size: str = "base"

    # Heartbeat
    heartbeat_enabled: bool = True
    heartbeat_interval_minutes: int = 30
    heartbeat_max_daily_messages: int = 5
    heartbeat_quiet_hours_start: int = 20  # 8 PM — fallback when no business_hours
    heartbeat_quiet_hours_end: int = 7  # 7 AM

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()
