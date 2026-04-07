"""Tests for centralized OAuth token refresh and error classification."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from backend.app.services.oauth import (
    _PERMANENT_OAUTH_ERROR_CODES,
    OAuthConfig,
    OAuthService,
    OAuthTokenData,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def oauth_svc() -> OAuthService:
    """Return a fresh OAuthService (no shared state with the module singleton)."""
    return OAuthService()


def _make_http_error(
    status_code: int,
    body: dict | None = None,
) -> httpx.HTTPStatusError:
    """Build a realistic httpx.HTTPStatusError with a JSON response body."""
    import json

    content = json.dumps(body or {}).encode()
    response = httpx.Response(
        status_code=status_code,
        content=content,
        headers={"content-type": "application/json"},
        request=httpx.Request("POST", "https://example.com/token"),
    )
    return httpx.HTTPStatusError(
        message=f"{status_code} error",
        request=response.request,
        response=response,
    )


# ---------------------------------------------------------------------------
# _is_permanent_refresh_failure
# ---------------------------------------------------------------------------


class TestIsPermanentRefreshFailure:
    def test_invalid_grant_is_permanent(self) -> None:
        error = _make_http_error(400, {"error": "invalid_grant"})
        assert OAuthService._is_permanent_refresh_failure(error) is True

    def test_invalid_client_is_permanent(self) -> None:
        error = _make_http_error(401, {"error": "invalid_client"})
        assert OAuthService._is_permanent_refresh_failure(error) is True

    def test_unauthorized_client_is_permanent(self) -> None:
        error = _make_http_error(400, {"error": "unauthorized_client"})
        assert OAuthService._is_permanent_refresh_failure(error) is True

    def test_server_error_is_transient(self) -> None:
        error = _make_http_error(500, {"error": "server_error"})
        assert OAuthService._is_permanent_refresh_failure(error) is False

    def test_timeout_is_transient(self) -> None:
        error = httpx.ConnectTimeout("Connection timed out")
        assert OAuthService._is_permanent_refresh_failure(error) is False

    def test_non_json_body_is_transient(self) -> None:
        response = httpx.Response(
            status_code=400,
            content=b"not json",
            headers={"content-type": "text/plain"},
            request=httpx.Request("POST", "https://example.com/token"),
        )
        error = httpx.HTTPStatusError(
            message="400 error", request=response.request, response=response
        )
        assert OAuthService._is_permanent_refresh_failure(error) is False

    def test_all_permanent_codes_covered(self) -> None:
        for code in _PERMANENT_OAUTH_ERROR_CODES:
            error = _make_http_error(400, {"error": code})
            assert OAuthService._is_permanent_refresh_failure(error) is True


# ---------------------------------------------------------------------------
# refresh_token
# ---------------------------------------------------------------------------


class TestRefreshToken:
    @pytest.mark.asyncio()
    async def test_happy_path(self, oauth_svc: OAuthService) -> None:
        """Refresh succeeds: new access token saved to DB."""
        stored = OAuthTokenData(
            access_token="old-at",
            refresh_token="rt-123",
            expires_at=time.time() - 100,
        )
        refreshed_body = {
            "access_token": "new-at",
            "refresh_token": "new-rt",
            "expires_in": 3600,
            "token_type": "Bearer",
        }

        mock_response = httpx.Response(
            200,
            json=refreshed_body,
            request=httpx.Request("POST", "https://oauth2.googleapis.com/token"),
        )
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        with (
            patch.object(oauth_svc, "load_token", return_value=stored),
            patch.object(oauth_svc, "save_token") as save_mock,
            patch.object(oauth_svc, "_get_http", return_value=mock_client),
            patch(
                "backend.app.services.oauth.get_oauth_config",
                return_value=OAuthConfig(
                    integration="google_calendar",
                    client_id="cid",
                    client_secret="csecret",
                    authorize_url="https://example.com/auth",
                    token_url="https://oauth2.googleapis.com/token",
                    scopes=[],
                ),
            ),
        ):
            result = await oauth_svc.refresh_token("user-1", "google_calendar")

        assert result is not None
        assert result.access_token == "new-at"
        assert result.refresh_token == "new-rt"
        assert result.expires_at > time.time()
        save_mock.assert_called_once()

    @pytest.mark.asyncio()
    async def test_rotates_refresh_token(self, oauth_svc: OAuthService) -> None:
        """When provider returns a new refresh_token, it should be saved."""
        stored = OAuthTokenData(
            access_token="old-at",
            refresh_token="old-rt",
            expires_at=time.time() - 100,
        )
        refreshed_body = {
            "access_token": "new-at",
            "refresh_token": "rotated-rt",
            "expires_in": 7200,
        }

        mock_response = httpx.Response(
            200,
            json=refreshed_body,
            request=httpx.Request("POST", "https://example.com/token"),
        )
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        with (
            patch.object(oauth_svc, "load_token", return_value=stored),
            patch.object(oauth_svc, "save_token") as save_mock,
            patch.object(oauth_svc, "_get_http", return_value=mock_client),
            patch(
                "backend.app.services.oauth.get_oauth_config",
                return_value=OAuthConfig(
                    integration="google_calendar",
                    client_id="cid",
                    client_secret="csecret",
                    authorize_url="https://example.com/auth",
                    token_url="https://example.com/token",
                    scopes=[],
                ),
            ),
        ):
            result = await oauth_svc.refresh_token("user-1", "google_calendar")

        assert result is not None
        assert result.refresh_token == "rotated-rt"
        save_mock.assert_called_once()

    @pytest.mark.asyncio()
    async def test_no_token_returns_none(self, oauth_svc: OAuthService) -> None:
        with patch.object(oauth_svc, "load_token", return_value=None):
            result = await oauth_svc.refresh_token("user-1", "google_calendar")
        assert result is None

    @pytest.mark.asyncio()
    async def test_no_refresh_token_returns_none(self, oauth_svc: OAuthService) -> None:
        stored = OAuthTokenData(access_token="at", refresh_token="")
        with patch.object(oauth_svc, "load_token", return_value=stored):
            result = await oauth_svc.refresh_token("user-1", "google_calendar")
        assert result is None

    @pytest.mark.asyncio()
    async def test_uses_expires_in(self, oauth_svc: OAuthService) -> None:
        """expires_at should be calculated from the provider's expires_in."""
        stored = OAuthTokenData(
            access_token="old-at",
            refresh_token="rt",
            expires_at=0,
        )
        refreshed_body = {"access_token": "new-at", "expires_in": 7200}

        mock_response = httpx.Response(
            200,
            json=refreshed_body,
            request=httpx.Request("POST", "https://example.com/token"),
        )
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        before = time.time()
        with (
            patch.object(oauth_svc, "load_token", return_value=stored),
            patch.object(oauth_svc, "save_token"),
            patch.object(oauth_svc, "_get_http", return_value=mock_client),
            patch(
                "backend.app.services.oauth.get_oauth_config",
                return_value=OAuthConfig(
                    integration="google_calendar",
                    client_id="cid",
                    client_secret="csecret",
                    authorize_url="https://example.com/auth",
                    token_url="https://example.com/token",
                    scopes=[],
                ),
            ),
        ):
            result = await oauth_svc.refresh_token("user-1", "google_calendar")

        assert result is not None
        assert result.expires_at >= before + 7200


# ---------------------------------------------------------------------------
# get_valid_token
# ---------------------------------------------------------------------------


class TestGetValidToken:
    @pytest.mark.asyncio()
    async def test_fresh_token_returned_as_is(self, oauth_svc: OAuthService) -> None:
        fresh = OAuthTokenData(
            access_token="at-good",
            refresh_token="rt",
            expires_at=time.time() + 3600,
        )
        with patch.object(oauth_svc, "load_token", return_value=fresh):
            result = await oauth_svc.get_valid_token("user-1", "google_calendar")
        assert result is not None
        assert result.access_token == "at-good"

    @pytest.mark.asyncio()
    async def test_no_token_returns_none(self, oauth_svc: OAuthService) -> None:
        with patch.object(oauth_svc, "load_token", return_value=None):
            result = await oauth_svc.get_valid_token("user-1", "google_calendar")
        assert result is None

    @pytest.mark.asyncio()
    async def test_expired_no_refresh_returns_none(self, oauth_svc: OAuthService) -> None:
        expired = OAuthTokenData(
            access_token="at",
            refresh_token="",
            expires_at=time.time() - 100,
        )
        with patch.object(oauth_svc, "load_token", return_value=expired):
            result = await oauth_svc.get_valid_token("user-1", "google_calendar")
        assert result is None

    @pytest.mark.asyncio()
    async def test_expired_refresh_succeeds(self, oauth_svc: OAuthService) -> None:
        expired = OAuthTokenData(
            access_token="old-at",
            refresh_token="rt",
            expires_at=time.time() - 100,
        )
        refreshed = OAuthTokenData(
            access_token="new-at",
            refresh_token="rt",
            expires_at=time.time() + 3600,
        )
        with (
            patch.object(oauth_svc, "load_token", return_value=expired),
            patch.object(
                oauth_svc, "refresh_token", new_callable=AsyncMock, return_value=refreshed
            ),
        ):
            result = await oauth_svc.get_valid_token("user-1", "google_calendar")
        assert result is not None
        assert result.access_token == "new-at"

    @pytest.mark.asyncio()
    async def test_permanent_failure_deletes_token(self, oauth_svc: OAuthService) -> None:
        expired = OAuthTokenData(
            access_token="old-at",
            refresh_token="rt",
            expires_at=time.time() - 100,
        )
        perm_error = _make_http_error(400, {"error": "invalid_grant"})

        with (
            patch.object(oauth_svc, "load_token", return_value=expired),
            patch.object(
                oauth_svc, "refresh_token", new_callable=AsyncMock, side_effect=perm_error
            ),
            patch.object(oauth_svc, "delete_token") as delete_mock,
            patch.object(oauth_svc, "_notify_reauth_needed", new_callable=AsyncMock) as notify_mock,
        ):
            result = await oauth_svc.get_valid_token("user-1", "google_calendar")

        assert result is None
        delete_mock.assert_called_once_with("user-1", "google_calendar")
        notify_mock.assert_called_once_with("user-1", "google_calendar")

    @pytest.mark.asyncio()
    async def test_transient_failure_preserves_token(self, oauth_svc: OAuthService) -> None:
        expired = OAuthTokenData(
            access_token="old-at",
            refresh_token="rt",
            expires_at=time.time() - 100,
        )
        transient_error = httpx.ConnectTimeout("Connection timed out")

        with (
            patch.object(oauth_svc, "load_token", return_value=expired),
            patch.object(
                oauth_svc, "refresh_token", new_callable=AsyncMock, side_effect=transient_error
            ),
            patch.object(oauth_svc, "delete_token") as delete_mock,
        ):
            result = await oauth_svc.get_valid_token("user-1", "google_calendar")

        assert result is None
        delete_mock.assert_not_called()


# ---------------------------------------------------------------------------
# _notify_reauth_needed
# ---------------------------------------------------------------------------


class TestNotifyReauthNeeded:
    @pytest.mark.asyncio()
    async def test_notification_failure_does_not_crash(self, oauth_svc: OAuthService) -> None:
        """Notification errors should be swallowed silently."""
        with patch(
            "backend.app.services.oauth.db_session",
            side_effect=RuntimeError("db down"),
        ):
            # Should not raise
            await oauth_svc._notify_reauth_needed("user-1", "google_calendar")

    @pytest.mark.asyncio()
    async def test_sends_via_bus_when_route_exists(self, oauth_svc: OAuthService) -> None:
        """Should publish an outbound message when a channel route exists."""
        mock_route = MagicMock()
        mock_route.channel = "telegram"
        mock_route.channel_identifier = "12345"

        mock_db = MagicMock()
        mock_db.__enter__ = MagicMock(return_value=mock_db)
        mock_db.__exit__ = MagicMock(return_value=False)
        mock_db.execute.return_value.scalars.return_value.first.return_value = mock_route

        mock_bus = AsyncMock()

        with (
            patch("backend.app.services.oauth.db_session", return_value=mock_db),
            patch("backend.app.services.oauth.select"),
            patch("backend.app.bus.message_bus", mock_bus),
        ):
            await oauth_svc._notify_reauth_needed("user-1", "google_calendar")

        mock_bus.publish_outbound.assert_called_once()
        msg = mock_bus.publish_outbound.call_args[0][0]
        assert msg.channel == "telegram"
        assert msg.chat_id == "12345"
        assert "Google Calendar" in msg.content
        assert "expired" in msg.content
