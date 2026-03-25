"""Tests for config field validation and startup warnings."""

import logging

import pytest
from pydantic import ValidationError

from backend.app.config import Settings, log_config_warnings


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
        s = Settings(encryption_key="a-secure-random-key-at-least-32-chars")
        assert log_config_warnings(s) == []

    def test_warns_missing_encryption_key(self) -> None:
        s = Settings(encryption_key="")
        warnings = log_config_warnings(s)
        assert any("encryption_key" in w for w in warnings)

    def test_warns_short_encryption_key(self) -> None:
        s = Settings(encryption_key="short")
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
