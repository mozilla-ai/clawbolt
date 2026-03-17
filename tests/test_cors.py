from backend.app.config import Settings


def test_default_cors_origins_are_restrictive() -> None:
    """Default CORS origins must not be the wildcard '*'."""
    s = Settings(
        _env_file=None,
        database_url="postgresql://test:test@localhost/test",
        telegram_bot_token="",
        telegram_webhook_secret="",
    )
    origins = s.cors_origins.split(",")
    assert "*" not in origins, "Default CORS origins must not contain '*'"
    assert all(o.startswith("http") for o in origins), "Each origin must be a valid URL"


def test_cors_origins_can_be_overridden() -> None:
    """Operators can still set explicit origins via env/config."""
    s = Settings(
        _env_file=None,
        database_url="postgresql://test:test@localhost/test",
        cors_origins="https://app.example.com,https://admin.example.com",
        telegram_bot_token="",
        telegram_webhook_secret="",
    )
    origins = s.cors_origins.split(",")
    assert origins == ["https://app.example.com", "https://admin.example.com"]
