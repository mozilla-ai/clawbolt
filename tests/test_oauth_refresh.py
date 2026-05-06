"""Tests for centralized OAuth token refresh and error classification."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from backend.app.services.oauth import (
    _PERMANENT_OAUTH_ERROR_CODES,
    OAuthConfig,
    OAuthService,
    OAuthTokenData,
    _refresh_lock_key,
    _try_acquire_advisory_lock_async,
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
            patch.object(oauth_svc, "load_token", new_callable=AsyncMock, return_value=stored),
            patch.object(oauth_svc, "save_token", new_callable=AsyncMock) as save_mock,
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
            patch.object(oauth_svc, "load_token", new_callable=AsyncMock, return_value=stored),
            patch.object(oauth_svc, "save_token", new_callable=AsyncMock) as save_mock,
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
        with patch.object(oauth_svc, "load_token", new_callable=AsyncMock, return_value=None):
            result = await oauth_svc.refresh_token("user-1", "google_calendar")
        assert result is None

    @pytest.mark.asyncio()
    async def test_no_refresh_token_returns_none(self, oauth_svc: OAuthService) -> None:
        stored = OAuthTokenData(access_token="at", refresh_token="")
        with patch.object(oauth_svc, "load_token", new_callable=AsyncMock, return_value=stored):
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
            patch.object(oauth_svc, "load_token", new_callable=AsyncMock, return_value=stored),
            patch.object(oauth_svc, "save_token", new_callable=AsyncMock),
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

    @pytest.mark.asyncio()
    async def test_uses_absolute_expires_at(self, oauth_svc: OAuthService) -> None:
        """When provider returns expires_at (absolute timestamp), use it directly."""
        stored = OAuthTokenData(access_token="old-at", refresh_token="rt", expires_at=0)
        absolute_ts = time.time() + 86400  # 24 hours from now
        refreshed_body = {"access_token": "new-at", "expires_at": absolute_ts}

        mock_response = httpx.Response(
            200,
            json=refreshed_body,
            request=httpx.Request("POST", "https://example.com/token"),
        )
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        with (
            patch.object(oauth_svc, "load_token", new_callable=AsyncMock, return_value=stored),
            patch.object(oauth_svc, "save_token", new_callable=AsyncMock),
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
        assert result.expires_at == pytest.approx(absolute_ts, abs=1)

    @pytest.mark.asyncio()
    async def test_missing_expiry_treated_as_non_expiring(self, oauth_svc: OAuthService) -> None:
        """When provider omits both expires_in and expires_at, token never expires."""
        stored = OAuthTokenData(
            access_token="old-at",
            refresh_token="rt",
            expires_at=time.time() - 100,  # was expired
        )
        refreshed_body = {"access_token": "new-at"}  # no expiry fields

        mock_response = httpx.Response(
            200,
            json=refreshed_body,
            request=httpx.Request("POST", "https://example.com/token"),
        )
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        with (
            patch.object(oauth_svc, "load_token", new_callable=AsyncMock, return_value=stored),
            patch.object(oauth_svc, "save_token", new_callable=AsyncMock),
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
        assert result.expires_at == 0.0
        assert result.is_expired() is False


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
        with patch.object(oauth_svc, "load_token", new_callable=AsyncMock, return_value=fresh):
            result = await oauth_svc.get_valid_token("user-1", "google_calendar")
        assert result is not None
        assert result.access_token == "at-good"

    @pytest.mark.asyncio()
    async def test_no_token_returns_none(self, oauth_svc: OAuthService) -> None:
        with patch.object(oauth_svc, "load_token", new_callable=AsyncMock, return_value=None):
            result = await oauth_svc.get_valid_token("user-1", "google_calendar")
        assert result is None

    @pytest.mark.asyncio()
    async def test_expired_no_refresh_returns_none(self, oauth_svc: OAuthService) -> None:
        expired = OAuthTokenData(
            access_token="at",
            refresh_token="",
            expires_at=time.time() - 100,
        )
        with patch.object(oauth_svc, "load_token", new_callable=AsyncMock, return_value=expired):
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
            patch.object(oauth_svc, "load_token", new_callable=AsyncMock, return_value=expired),
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
            patch.object(oauth_svc, "load_token", new_callable=AsyncMock, return_value=expired),
            patch.object(
                oauth_svc, "refresh_token", new_callable=AsyncMock, side_effect=perm_error
            ),
            patch.object(oauth_svc, "delete_token", new_callable=AsyncMock) as delete_mock,
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
            patch.object(oauth_svc, "load_token", new_callable=AsyncMock, return_value=expired),
            patch.object(
                oauth_svc, "refresh_token", new_callable=AsyncMock, side_effect=transient_error
            ),
            patch.object(oauth_svc, "delete_token", new_callable=AsyncMock) as delete_mock,
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
            "backend.app.services.oauth.AsyncSessionLocal",
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

        mock_result = MagicMock()
        mock_result.scalars.return_value.first.return_value = mock_route

        mock_db = AsyncMock()
        mock_db.execute.return_value = mock_result
        mock_db.close = AsyncMock()

        mock_bus = AsyncMock()

        with (
            patch("backend.app.services.oauth.AsyncSessionLocal", return_value=mock_db),
            patch("backend.app.bus.message_bus", mock_bus),
        ):
            await oauth_svc._notify_reauth_needed("user-1", "google_calendar")

        mock_bus.publish_outbound.assert_called_once()
        msg = mock_bus.publish_outbound.call_args[0][0]
        assert msg.channel == "telegram"
        assert msg.chat_id == "12345"
        assert "Google Calendar" in msg.content
        assert "expired" in msg.content


# ---------------------------------------------------------------------------
# Bounded advisory-lock acquisition
# ---------------------------------------------------------------------------


class TestAdvisoryLockBounded:
    """Regression for a prod hang: ``pg_advisory_lock`` blocked the event loop
    indefinitely when the lock was held by an orphaned session-scoped lock from
    a dropped connection. The acquire path must fail fast under contention."""

    @pytest.mark.asyncio()
    async def test_async_acquire_returns_false_when_lock_held_by_peer(
        self, _pg_async_engine: AsyncEngine
    ) -> None:
        lock_key = _refresh_lock_key("contended-user", "google_calendar")
        # Open a peer async connection that holds the advisory lock for
        # the duration of the test, simulating the orphaned-lock scenario.
        peer = await _pg_async_engine.connect()
        try:
            await peer.execute(text("SELECT pg_advisory_lock(hashtext(:k))"), {"k": lock_key})
            await peer.commit()

            # The async helper operates on an ``AsyncConnection`` so the
            # event loop stays responsive during the bounded poll.
            db = await _pg_async_engine.connect()
            try:
                with patch("backend.app.services.oauth._LOCK_MAX_WAIT_S", 0.3):
                    start = time.monotonic()
                    acquired = await _try_acquire_advisory_lock_async(db, lock_key)
                    elapsed = time.monotonic() - start
            finally:
                await db.close()

            assert acquired is False
            # Bounded wait, not the multi-hour pg_advisory_lock freeze.
            assert elapsed < 1.0
        finally:
            await peer.execute(text("SELECT pg_advisory_unlock_all()"))
            await peer.commit()
            await peer.close()

    @pytest.mark.asyncio()
    async def test_async_acquire_succeeds_when_lock_free(
        self, _pg_async_engine: AsyncEngine
    ) -> None:
        lock_key = _refresh_lock_key("free-user", "google_calendar")
        db = await _pg_async_engine.connect()
        try:
            acquired = await _try_acquire_advisory_lock_async(db, lock_key)
            assert acquired is True
            await db.execute(text("SELECT pg_advisory_unlock(hashtext(:k))"), {"k": lock_key})
            await db.commit()
        finally:
            await db.close()


# ---------------------------------------------------------------------------
# Concurrency regression: refresh_token advisory-lock serialization (issue #1145)
# ---------------------------------------------------------------------------


class TestRefreshTokenLockSerialization:
    """Regression tests for the OAuth refresh advisory lock.

    The lock around ``refresh_token`` exists so two concurrent callers
    racing a 401 do not both POST to the provider. Providers that rotate
    the refresh token (Google, QuickBooks) invalidate the loser's
    newly-issued credential when the second POST lands, and the loser's
    write also clobbers the winner's persisted ``refresh_token`` row.
    Holding a session-scoped Postgres advisory lock keyed on
    ``(user_id, integration)`` from before the read through after the
    save lets the late callers re-read the freshly rotated token and
    skip the redundant HTTP call.

    The invariant is load-bearing: it is the only thing preventing
    duplicate upstream traffic and stomped refresh tokens under the
    realistic case of two workers each handling an inbound that needs
    the same user's calendar.

    Concurrency primitives:

    * Async test uses ``asyncio.gather`` against the production
      ``refresh_token`` coroutine.
    * Callback test uses ``asyncio.gather`` against the production
      ``build_on_refresh_callback`` closure.

    The lock helpers are exercised end-to-end via the public service
    methods, not in isolation, so a future refactor that bypasses the
    helpers entirely still has to hit this assertion.

    No timestamp-based assertions across tasks (per #1202): all
    coordination is via ``asyncio.Event``.
    """

    _N_CONCURRENT = 3
    # Generous bound so a slow CI runner does not flake the test.
    _TIMEOUT_S = 5.0

    @pytest.mark.asyncio()
    async def test_async_refresh_serializes_concurrent_callers(
        self, _pg_async_engine: AsyncEngine, oauth_svc: OAuthService
    ) -> None:
        """N concurrent ``refresh_token`` calls produce exactly one
        upstream POST and all callers receive the same rotated token.

        Mutation-test invariant: with the bug (advisory lock held on a
        ``Session`` whose ``commit()`` returns the connection to the
        pool), every caller's ``pg_try_advisory_lock`` returns True
        (locks are reentrant per PG session, and recycled connections
        carry the previous holder's lock with them) and every caller
        races to the upstream POST. With the fix (lock held on a
        dedicated ``Connection`` that stays pinned), only the first
        caller acquires; the others wait, then re-read the rotated
        token after the first releases and skip the HTTP call.
        """
        user_id = "lock-test-user"
        integration = "google_calendar"

        initial_token = OAuthTokenData(
            access_token="old-at",
            refresh_token="old-rt",
            expires_at=time.time() - 100,  # already expired
        )
        # ``persisted`` is the single source of truth that
        # ``_load_token_uncached`` consults. The first caller's
        # ``save_token`` swaps in the rotated value, so the late
        # callers see ``expires_at > now`` on their post-lock reload
        # and short-circuit without hitting the upstream.
        persisted: dict[str, OAuthTokenData] = {"current": initial_token}

        def _load_uncached(uid: str, ig: str) -> OAuthTokenData | None:
            return persisted["current"]

        def _save(uid: str, ig: str, token: OAuthTokenData) -> None:
            # Snapshot the token so subsequent reads see the rotated
            # value. ``OAuthTokenData`` is a dataclass; the production
            # code mutates the instance in place before saving, so we
            # must copy to avoid the late callers' reads picking up
            # the same shared object.
            persisted["current"] = OAuthTokenData(
                access_token=token.access_token,
                refresh_token=token.refresh_token,
                token_type=token.token_type,
                expires_at=token.expires_at,
                scopes=list(token.scopes),
                realm_id=token.realm_id,
                extra=dict(token.extra),
            )

        # The httpx mock blocks until ``release`` is set, so all N
        # callers are inside the critical section (or contending for
        # the lock) at the same moment. Exactly one should reach this
        # call; the rest must short-circuit on the post-lock reload.
        release = asyncio.Event()
        call_count = 0
        call_count_lock = asyncio.Lock()

        async def _slow_post(*args: object, **kwargs: object) -> httpx.Response:
            nonlocal call_count
            async with call_count_lock:
                call_count += 1
            await release.wait()
            return httpx.Response(
                200,
                json={
                    "access_token": "rotated-at",
                    "refresh_token": "rotated-rt",
                    "expires_in": 3600,
                },
                request=httpx.Request("POST", "https://example.com/token"),
            )

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(side_effect=_slow_post)

        config = OAuthConfig(
            integration=integration,
            client_id="cid",
            client_secret="csecret",
            authorize_url="https://example.com/auth",
            token_url="https://example.com/token",
            scopes=[],
        )

        async def _kick_release_after_first_call() -> None:
            # Wait until the first (and ideally only) caller has
            # entered the upstream POST, then unblock it. If multiple
            # callers reach the POST, they will all be waiting on the
            # same ``release`` event and ``call_count`` will reflect
            # the duplicate traffic.
            deadline = time.monotonic() + self._TIMEOUT_S
            while call_count < 1 and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            # Brief extra wait so a buggy second caller has time to
            # also reach the POST (and get counted) before we let the
            # first one finish. Without this, a fast happy path could
            # mask the race window.
            await asyncio.sleep(0.3)
            release.set()

        with (
            patch.object(oauth_svc, "_load_token_uncached", side_effect=_load_uncached),
            patch.object(oauth_svc, "save_token", side_effect=_save),
            patch.object(oauth_svc, "_get_http", return_value=mock_http),
            patch("backend.app.services.oauth.get_oauth_config", return_value=config),
        ):
            tasks = [
                asyncio.create_task(oauth_svc.refresh_token(user_id, integration))
                for _ in range(self._N_CONCURRENT)
            ]
            tasks.append(asyncio.create_task(_kick_release_after_first_call()))
            results = await asyncio.wait_for(
                asyncio.gather(*tasks[: self._N_CONCURRENT]),
                timeout=self._TIMEOUT_S,
            )
            await tasks[-1]

        assert call_count == 1, (
            f"expected exactly one upstream POST under the lock, got {call_count}; "
            "the advisory lock failed to serialize concurrent refreshes"
        )
        assert all(r is not None for r in results), (
            "every caller should return the rotated token; "
            f"got {[None if r is None else 'token' for r in results]}"
        )
        rotated = persisted["current"]
        assert rotated.access_token == "rotated-at"
        assert rotated.refresh_token == "rotated-rt"
        for idx, r in enumerate(results):
            assert r is not None
            assert r.access_token == "rotated-at", (
                f"caller {idx} returned a different access_token "
                f"({r.access_token!r}); the lock allowed a racing rotation"
            )
            assert r.refresh_token == "rotated-rt", (
                f"caller {idx} returned a different refresh_token "
                f"({r.refresh_token!r}); the lock allowed a racing rotation"
            )

    @pytest.mark.asyncio()
    async def test_callback_serializes_concurrent_persists(
        self, _pg_engine: Engine, oauth_svc: OAuthService
    ) -> None:
        """N concurrent ``on_refresh`` callbacks for the same user serialize
        on the advisory lock: each runs the load+save under the lock so
        the rotated ``refresh_token`` field is preserved across overlapping
        provider-driven mid-call refreshes.
        """
        user_id = "lock-test-user-callback"
        integration = "quickbooks"

        # Each thread persists a unique rotated refresh_token so we
        # can verify that *every* persist saw a non-stale base row.
        # If two callbacks read the base row concurrently and both
        # save, the loser's save clobbers the winner's rotated value.
        # We assert the final saved row matches the last-running
        # thread's intent, which can only happen if persists were
        # serialized.
        base_token = OAuthTokenData(
            access_token="base-at",
            refresh_token="base-rt",
            expires_at=time.time() + 100,
        )
        persisted: dict[str, OAuthTokenData] = {"current": base_token}
        # Each entry records the ``refresh_token`` value a thread saw
        # when its post-lock ``_load_token_uncached`` ran. With the
        # lock working, only the first acquirer sees the original
        # ``base-rt``; subsequent acquirers see a peer's rotation.
        # With the bug, every thread reads the row before any save
        # completes, so every entry equals ``base-rt``.
        loaded_bases: list[str] = []
        record_lock = threading.Lock()

        # Hold each thread inside its critical section for a beat so
        # racing threads have time to also enter and observe the same
        # base. Without this, the GIL plus a fast in-memory save
        # could let threads finish their critical section before the
        # next one starts, masking the bug.
        hold_inside_critical_s = 0.05

        async def _load_uncached(uid: str, ig: str) -> OAuthTokenData | None:
            current = persisted["current"]
            snapshot = OAuthTokenData(
                access_token=current.access_token,
                refresh_token=current.refresh_token,
                token_type=current.token_type,
                expires_at=current.expires_at,
                scopes=list(current.scopes),
                realm_id=current.realm_id,
                extra=dict(current.extra),
            )
            with record_lock:
                loaded_bases.append(snapshot.refresh_token)
            return snapshot

        async def _save(uid: str, ig: str, token: OAuthTokenData) -> None:
            # Hold inside the critical section so a buggy peer that
            # bypasses the lock has a clean window to also observe
            # the pre-save state and append its own ``base-rt`` to
            # ``loaded_bases``.
            await asyncio.sleep(hold_inside_critical_s)
            with record_lock:
                persisted["current"] = OAuthTokenData(
                    access_token=token.access_token,
                    refresh_token=token.refresh_token,
                    token_type=token.token_type,
                    expires_at=token.expires_at,
                    scopes=list(token.scopes),
                    realm_id=token.realm_id,
                    extra=dict(token.extra),
                )

        start = asyncio.Event()

        async def _run_callback(idx: int) -> None:
            # Coordinate so all N threads call the public callback at
            # the same moment. Each callback opens its own lock
            # connection and contends for the advisory lock.
            await start.wait()
            new_at = f"rotated-at-{idx}"
            new_rt = f"rotated-rt-{idx}"
            cb = oauth_svc.build_on_refresh_callback(user_id, integration)
            await cb(new_at, new_rt, time.time() + 3600)

        # Apply the patches once on the shared service before
        # spawning tasks so every callback sees the same mocked state.
        with (
            patch.object(oauth_svc, "_load_token_uncached", side_effect=_load_uncached),
            patch.object(oauth_svc, "save_token", side_effect=_save),
        ):
            tasks = [asyncio.create_task(_run_callback(idx)) for idx in range(self._N_CONCURRENT)]
            start.set()
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=self._TIMEOUT_S * 2)

        # Every callback ran its post-lock load (none bailed out on
        # lock timeout).
        assert len(loaded_bases) == self._N_CONCURRENT, (
            f"expected {self._N_CONCURRENT} post-lock loads, got "
            f"{len(loaded_bases)}; loaded_bases={loaded_bases}. "
            "Some callbacks bailed out before reading."
        )

        # Acid test: exactly one thread loaded the original ``base-rt``.
        # With the bug, every thread loads ``base-rt`` (loads happen
        # before any save completes, so all see the pre-save state).
        # With the fix, only the first acquirer sees ``base-rt``; the
        # rest see a previous thread's rotation.
        base_observations = loaded_bases.count("base-rt")
        assert base_observations == 1, (
            f"expected exactly one thread to observe the original base "
            f"refresh_token, got {base_observations}; loaded_bases="
            f"{loaded_bases}. The advisory lock failed to serialize "
            f"on_refresh callbacks: every thread read the same stale "
            f"row before any thread's save landed."
        )

    @pytest.mark.asyncio()
    async def test_async_helper_with_session_input_is_documented_misuse(
        self, _pg_async_engine: AsyncEngine
    ) -> None:
        """Same-connection coupling check: the lock helper guarantees
        nothing if the caller passes an ``AsyncSession`` (whose
        ``commit()`` returns the underlying connection to the pool).

        This is the production bug encoded as an invariant: the
        production refresh path now passes ``async_engine.connect()``
        (an ``AsyncConnection``), and any future refactor that swaps
        it back to ``AsyncSessionLocal()`` re-introduces the bug.
        Mirrors the same-connection-coupling check in
        ``test_inbound_recovery.py::test_unlock_on_different_connection_is_a_no_op``.

        Demonstrates the failure mode by calling the helper twice on
        two ``AsyncSession`` instances bound to a shared engine: the
        second ``pg_try_advisory_lock`` returns ``True`` even though
        the first session never explicitly released, because the
        underlying connection was returned to the pool by the
        intervening commit.
        """
        from sqlalchemy.ext.asyncio import async_sessionmaker

        lock_key = _refresh_lock_key("session-misuse-user", "google_calendar")
        # pool_size=2 + max_overflow=0 keeps the pool small enough that
        # the test reliably observes the same connection getting
        # recycled between the two sessions. The point of this test is
        # to encode the exact failure mode the production fix avoids,
        # so we keep the setup minimal and close to the bug.
        small_engine = create_async_engine(
            _pg_async_engine.url,
            pool_size=2,
            max_overflow=0,
        )
        try:
            factory = async_sessionmaker(bind=small_engine, expire_on_commit=False)
            s1 = factory()
            s2 = factory()
            try:
                acquired_1 = await _try_acquire_advisory_lock_async(s1, lock_key)
                # ``s1.commit()`` inside the helper returned the
                # underlying connection to the pool. ``s2`` now picks
                # up the same connection on its first execute. Locks
                # are reentrant per PG session, so the next acquire
                # call on ``s2`` returns True even though we have not
                # explicitly released. This is the bug.
                acquired_2 = await _try_acquire_advisory_lock_async(s2, lock_key)
                assert acquired_1 is True
                assert acquired_2 is True, (
                    "Postgres advisory lock semantics changed: an AsyncSession "
                    "returning its connection to the pool no longer lets a "
                    "peer session re-acquire. If this is now False, the "
                    "production fix may be unnecessary; investigate before "
                    "deleting it."
                )
            finally:
                # Best-effort cleanup. ``pg_advisory_unlock_all`` on a
                # raw connection drops every lock held by that PG
                # session regardless of which session originally
                # acquired it.
                async with small_engine.connect() as cleanup:
                    await cleanup.execute(text("SELECT pg_advisory_unlock_all()"))
                    await cleanup.commit()
                await s1.close()
                await s2.close()
        finally:
            await small_engine.dispose()
