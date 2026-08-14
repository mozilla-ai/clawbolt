"""Tests for OAuth state token key separation."""

import jwt
import pytest
from fastapi import HTTPException

from backend.app.config import settings
from backend.app.routers.google_oauth import (
    _create_state_token,
    _get_state_signing_key,
    _validate_state_token,
)


class TestOAuthStateKeySeparation:
    def test_state_key_differs_from_jwt_secret(self) -> None:
        """State signing key should differ from the main JWT secret."""
        state_key = _get_state_signing_key()
        assert state_key != settings.jwt_secret

    def test_state_key_is_deterministic(self) -> None:
        """Same jwt_secret should produce the same state key."""
        key1 = _get_state_signing_key()
        key2 = _get_state_signing_key()
        assert key1 == key2

    def test_create_and_validate_state_token(self) -> None:
        """State token should round-trip through create/validate."""
        token = _create_state_token()
        _validate_state_token(token)  # Should not raise

    def test_jwt_secret_cannot_decode_state_token(self) -> None:
        """A state token signed with the derived key should not decode with jwt_secret."""
        token = _create_state_token()
        with pytest.raises(jwt.InvalidSignatureError):
            jwt.decode(
                token,
                settings.jwt_secret,
                algorithms=[settings.jwt_algorithm],
            )

    def test_access_token_rejected_as_state(self) -> None:
        """An access token should be rejected when validated as a state token."""
        from backend.app.auth.jwt_auth import create_access_token

        access_token = create_access_token(user_id="test-user-id")
        with pytest.raises(HTTPException) as exc_info:
            _validate_state_token(access_token)
        assert exc_info.value.status_code == 400

    def test_expired_state_raises(self) -> None:
        """Expired state tokens should raise 400."""
        import datetime

        payload = {
            "type": "oauth_state",
            "iat": datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=10),
            "exp": datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=5),
        }
        token = jwt.encode(
            payload,
            _get_state_signing_key(),
            algorithm=settings.jwt_algorithm,
        )
        with pytest.raises(HTTPException) as exc_info:
            _validate_state_token(token)
        assert exc_info.value.status_code == 400
        assert "expired" in exc_info.value.detail.lower()
