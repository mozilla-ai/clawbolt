"""Tests for premium config validation."""

from backend.app.config import Settings


class TestSettings:
    def test_defaults(self) -> None:
        """Default settings should be safe for local development."""
        s = Settings(
            _env_file=None,
        )
        assert s.jwt_secret == "change-me-in-production"
        assert s.jwt_access_token_expire_minutes == 15
        assert s.jwt_refresh_token_expire_days == 30
        assert s.app_base_url == "http://localhost:8000"

    def test_admin_user_ids_empty(self) -> None:
        """Empty admin_user_ids_raw should return empty set."""
        s = Settings(admin_user_ids_raw="", _env_file=None)
        assert s.admin_user_ids == set()

    def test_admin_user_ids_single(self) -> None:
        """Single admin ID should be parsed correctly."""
        s = Settings(
            admin_user_ids_raw="google_abc123",
            _env_file=None,
        )
        assert s.admin_user_ids == {"google_abc123"}

    def test_admin_user_ids_multiple(self) -> None:
        """Multiple comma-separated admin IDs should be parsed."""
        s = Settings(
            admin_user_ids_raw="id1, id2, id3",
            _env_file=None,
        )
        assert s.admin_user_ids == {"id1", "id2", "id3"}

    def test_admin_user_ids_strips_whitespace(self) -> None:
        """Whitespace around IDs should be stripped."""
        s = Settings(
            admin_user_ids_raw="  id1 ,  id2  ,  ",
            _env_file=None,
        )
        assert s.admin_user_ids == {"id1", "id2"}

    def test_configurable_defaults(self) -> None:
        """New configurable settings should have sensible defaults."""
        s = Settings(_env_file=None)
        assert s.auth_rate_limit_max_requests == 10
        assert s.auth_rate_limit_window_seconds == 60
        assert s.oauth_state_expiry_minutes == 5
        assert s.inactive_warn_months == 11
        assert s.inactive_delete_months == 12

    def test_configurable_overrides(self) -> None:
        """New configurable settings should accept overrides."""
        s = Settings(
            auth_rate_limit_max_requests=20,
            auth_rate_limit_window_seconds=120,
            oauth_state_expiry_minutes=10,
            inactive_warn_months=9,
            inactive_delete_months=10,
            _env_file=None,
        )
        assert s.auth_rate_limit_max_requests == 20
        assert s.auth_rate_limit_window_seconds == 120
        assert s.oauth_state_expiry_minutes == 10
        assert s.inactive_warn_months == 9
        assert s.inactive_delete_months == 10

    def test_kms_key_arn_whitespace_is_treated_as_unset(self) -> None:
        """A whitespace-only KMS_KEY_ARN should normalize to empty so the
        plugin sees the dormancy signal rather than constructing a
        provider that crashes on the first KMS call."""
        s = Settings(kms_key_arn="   ", _env_file=None)
        assert s.kms_key_arn == ""

    def test_admin_email_normalized_to_lowercase(self) -> None:
        """admin_email should be lowercased and stripped (F-26)."""
        s = Settings(admin_email="  Admin@Example.COM  ", _env_file=None)
        assert s.admin_email == "admin@example.com"

    def test_admin_email_empty_preserved(self) -> None:
        """Empty admin_email stays empty."""
        s = Settings(admin_email="", _env_file=None)
        assert s.admin_email == ""

    def test_kms_key_arn_real_value_preserved(self) -> None:
        """A real ARN passes through the validator unchanged (modulo
        leading/trailing whitespace from copy-paste)."""
        arn = "arn:aws:kms:us-east-1:123456789012:key/abcd-1234"
        s = Settings(kms_key_arn=f"  {arn}  ", _env_file=None)
        assert s.kms_key_arn == arn
