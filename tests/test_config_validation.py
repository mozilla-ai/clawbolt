"""Tests for config field validation and startup warnings."""

import logging

import pytest
from pydantic import SecretStr, ValidationError

from backend.app.config import (
    Settings,
    log_config_warnings,
    resolve_imessage_backend,
    validate_imessage_backend,
)


class TestFieldConstraints:
    """Pydantic Field constraints reject invalid values at construction time."""

    def test_max_tool_rounds_rejects_zero(self) -> None:
        with pytest.raises(ValidationError):
            Settings(max_tool_rounds=0)

    def test_max_tool_rounds_rejects_negative(self) -> None:
        with pytest.raises(ValidationError):
            Settings(max_tool_rounds=-1)

    def test_max_tool_rounds_accepts_one(self) -> None:
        s = Settings(max_tool_rounds=1)
        assert s.max_tool_rounds == 1

    def test_message_batch_window_ms_rejects_zero(self) -> None:
        with pytest.raises(ValidationError):
            Settings(message_batch_window_ms=0)

    def test_message_batch_window_ms_rejects_below_minimum(self) -> None:
        with pytest.raises(ValidationError):
            Settings(message_batch_window_ms=50)

    def test_message_batch_window_ms_accepts_minimum(self) -> None:
        s = Settings(message_batch_window_ms=100)
        assert s.message_batch_window_ms == 100

    def test_llm_max_tokens_agent_rejects_zero(self) -> None:
        with pytest.raises(ValidationError):
            Settings(llm_max_tokens_agent=0)

    def test_llm_max_tokens_agent_accepts_one(self) -> None:
        s = Settings(llm_max_tokens_agent=1)
        assert s.llm_max_tokens_agent == 1

    def test_llm_max_retries_rejects_zero(self) -> None:
        with pytest.raises(ValidationError):
            Settings(llm_max_retries=0)

    def test_llm_max_retries_accepts_one(self) -> None:
        s = Settings(llm_max_retries=1)
        assert s.llm_max_retries == 1

    def test_llm_max_retries_default_is_three(self) -> None:
        s = Settings()
        assert s.llm_max_retries == 3

    def test_http_timeout_rejects_zero(self) -> None:
        with pytest.raises(ValidationError):
            Settings(http_timeout_seconds=0)

    def test_heartbeat_quiet_hours_start_rejects_24(self) -> None:
        with pytest.raises(ValidationError):
            Settings(heartbeat_quiet_hours_start=24)

    def test_heartbeat_quiet_hours_end_rejects_negative(self) -> None:
        with pytest.raises(ValidationError):
            Settings(heartbeat_quiet_hours_end=-1)

    def test_heartbeat_quiet_hours_accepts_bounds(self) -> None:
        s = Settings(heartbeat_quiet_hours_start=0, heartbeat_quiet_hours_end=23)
        assert s.heartbeat_quiet_hours_start == 0
        assert s.heartbeat_quiet_hours_end == 23

    def test_defaults_are_valid(self) -> None:
        """The default Settings() should construct without errors."""
        s = Settings()
        assert s.max_tool_rounds == 10
        assert s.message_batch_window_ms == 1500


class TestLogConfigWarnings:
    """log_config_warnings emits warnings for unusual but valid values."""

    def test_no_warnings_with_defaults(self) -> None:
        # Explicit overrides for env vars that leak from a local .env into
        # tests (LINQ_*, BLUEBUBBLES_*) so this test is deterministic in
        # any developer's shell.
        s = Settings(
            encryption_key=SecretStr("a-secure-random-key-at-least-32-chars"),
            linq_api_token="",
            linq_from_number="",
            bluebubbles_server_url="",
            bluebubbles_password="",
            bluebubbles_imessage_address="",
        )
        assert log_config_warnings(s) == []

    def test_warns_missing_encryption_key(self) -> None:
        s = Settings(encryption_key=SecretStr(""))
        warnings = log_config_warnings(s)
        assert any("encryption_key" in w for w in warnings)

    def test_warns_short_encryption_key(self) -> None:
        s = Settings(encryption_key=SecretStr("short"))
        warnings = log_config_warnings(s)
        assert any("encryption_key" in w for w in warnings)

    def test_warns_high_max_tool_rounds(self) -> None:
        s = Settings(max_tool_rounds=100)
        warnings = log_config_warnings(s)
        assert any("max_tool_rounds" in w for w in warnings)

    def test_warns_high_batch_window(self) -> None:
        s = Settings(message_batch_window_ms=15_000)
        warnings = log_config_warnings(s)
        assert any("message_batch_window_ms" in w for w in warnings)

    def test_warns_low_llm_max_tokens(self) -> None:
        s = Settings(llm_max_tokens_agent=10)
        warnings = log_config_warnings(s)
        assert any("llm_max_tokens_agent" in w for w in warnings)

    def test_warns_trim_target_exceeds_max_input(self) -> None:
        s = Settings(max_input_tokens=1000, context_trim_target_tokens=2000)
        warnings = log_config_warnings(s)
        assert any("context_trim_target_tokens" in w for w in warnings)

    def test_logs_warnings(self, caplog: pytest.LogCaptureFixture) -> None:
        s = Settings(max_tool_rounds=100)
        with caplog.at_level(logging.WARNING):
            log_config_warnings(s)
        assert any("max_tool_rounds" in r.message for r in caplog.records)


class TestIMessageBackend:
    """validate_imessage_backend rejects double-configuration; resolve_imessage_backend picks the active one."""

    def test_resolve_none_when_nothing_set(self) -> None:
        s = Settings(linq_api_token="", bluebubbles_server_url="", bluebubbles_password="")
        assert resolve_imessage_backend(s) is None

    def test_resolve_linq_when_only_linq_set(self) -> None:
        s = Settings(
            linq_api_token="tok",
            bluebubbles_server_url="",
            bluebubbles_password="",
        )
        assert resolve_imessage_backend(s) == "linq"

    def test_resolve_bluebubbles_when_only_bluebubbles_set(self) -> None:
        s = Settings(
            linq_api_token="",
            bluebubbles_server_url="https://mac.ngrok.io",
            bluebubbles_password="p",
        )
        assert resolve_imessage_backend(s) == "bluebubbles"

    def test_bluebubbles_requires_both_url_and_password(self) -> None:
        # Partial config is not "configured" - resolver returns None.
        partial_url_only = Settings(
            linq_api_token="",
            bluebubbles_server_url="https://mac.ngrok.io",
            bluebubbles_password="",
        )
        partial_pw_only = Settings(
            linq_api_token="",
            bluebubbles_server_url="",
            bluebubbles_password="p",
        )
        assert resolve_imessage_backend(partial_url_only) is None
        assert resolve_imessage_backend(partial_pw_only) is None

    def test_validate_accepts_only_linq(self) -> None:
        s = Settings(linq_api_token="tok", bluebubbles_server_url="", bluebubbles_password="")
        validate_imessage_backend(s)  # must not raise

    def test_validate_accepts_only_bluebubbles(self) -> None:
        s = Settings(
            linq_api_token="",
            bluebubbles_server_url="https://mac.ngrok.io",
            bluebubbles_password="p",
        )
        validate_imessage_backend(s)

    def test_validate_accepts_neither(self) -> None:
        s = Settings(linq_api_token="", bluebubbles_server_url="", bluebubbles_password="")
        validate_imessage_backend(s)

    def test_validate_rejects_both_configured(self) -> None:
        s = Settings(
            linq_api_token="tok",
            bluebubbles_server_url="https://mac.ngrok.io",
            bluebubbles_password="p",
        )
        with pytest.raises(RuntimeError, match="iMessage"):
            validate_imessage_backend(s)


class TestIMessageAddressWarning:
    """log_config_warnings flags a configured iMessage backend with empty address."""

    def test_warns_linq_configured_without_from_number(self) -> None:
        s = Settings(linq_api_token="tok", linq_from_number="")
        warnings = log_config_warnings(s)
        assert any("LINQ_FROM_NUMBER is empty" in w for w in warnings)

    def test_no_warning_when_linq_from_number_set(self) -> None:
        s = Settings(linq_api_token="tok", linq_from_number="+15551234567")
        warnings = log_config_warnings(s)
        assert not any("LINQ_FROM_NUMBER" in w for w in warnings)

    def test_warns_bluebubbles_configured_without_imessage_address(self) -> None:
        s = Settings(
            bluebubbles_server_url="https://mac.ngrok.io",
            bluebubbles_password="p",
            bluebubbles_imessage_address="",
        )
        warnings = log_config_warnings(s)
        assert any("BLUEBUBBLES_IMESSAGE_ADDRESS is empty" in w for w in warnings)

    def test_no_warning_when_bluebubbles_imessage_address_set(self) -> None:
        s = Settings(
            bluebubbles_server_url="https://mac.ngrok.io",
            bluebubbles_password="p",
            bluebubbles_imessage_address="clawbolt@icloud.com",
        )
        warnings = log_config_warnings(s)
        assert not any("BLUEBUBBLES_IMESSAGE_ADDRESS" in w for w in warnings)

    def test_no_warning_when_no_imessage_backend_configured(self) -> None:
        s = Settings(linq_api_token="", bluebubbles_server_url="", bluebubbles_password="")
        warnings = log_config_warnings(s)
        assert not any(
            "LINQ_FROM_NUMBER" in w or "BLUEBUBBLES_IMESSAGE_ADDRESS" in w for w in warnings
        )
