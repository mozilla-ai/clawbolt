from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "postgresql://backshop:backshop@localhost:5432/backshop"
    cors_origins: str = "*"
    jwt_secret: str = "change-me-in-production"
    jwt_expiry_minutes: int = 15
    premium_plugin: str | None = None

    # Twilio
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_phone_number: str = ""
    twilio_validate_signatures: bool = True

    # LLM
    llm_provider: str = "openai"
    llm_model: str = "gpt-4o"
    vision_model: str = "gpt-4o"

    # Storage
    storage_provider: str = "dropbox"  # "dropbox" or "google_drive"
    dropbox_access_token: str = ""
    google_drive_credentials_json: str = ""

    # Whisper
    whisper_model_size: str = "base"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}


settings = Settings()
