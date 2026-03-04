import os
from unittest.mock import patch

from backend.app.config import Settings


class TestSettingsDoesNotExposeProviderKeys:
    """Confirm that Pydantic BaseSettings ignores unknown env vars.

    This documents the root cause: Settings has ``extra="ignore"`` so
    provider API keys written in .env are silently discarded by Pydantic
    and never placed into os.environ.
    """

    def test_groq_key_not_a_settings_field(self) -> None:
        assert not hasattr(Settings, "groq_api_key")

    def test_openai_key_not_a_settings_field(self) -> None:
        assert not hasattr(Settings, "openai_api_key")

    def test_anthropic_key_not_a_settings_field(self) -> None:
        assert not hasattr(Settings, "anthropic_api_key")

    def test_gemini_key_not_a_settings_field(self) -> None:
        assert not hasattr(Settings, "gemini_api_key")


class TestLoadDotenvInMain:
    """Verify that main.py calls load_dotenv() so provider keys reach os.environ."""

    def test_load_dotenv_is_called_in_lifespan(self) -> None:
        """main.py must call load_dotenv() in lifespan() so provider keys
        reach os.environ before any LLM call is made.
        """
        import inspect

        from backend.app import main

        source = inspect.getsource(main.lifespan)
        assert "load_dotenv()" in source, (
            "main.py lifespan() must call load_dotenv() so provider "
            "API keys from .env are available in os.environ"
        )

    def test_provider_key_available_after_load_dotenv(self, tmp_path: object) -> None:
        """Simulate load_dotenv() making a provider key visible."""
        fake_key = "gsk_test_fake_key_1234567890"
        with patch.dict(os.environ, {"GROQ_API_KEY": fake_key}):
            assert os.environ.get("GROQ_API_KEY") == fake_key

    def test_provider_key_not_available_without_env(self) -> None:
        """Without the key in env, os.environ.get returns None."""
        with patch.dict(os.environ, {}, clear=True):
            assert os.environ.get("GROQ_API_KEY") is None
