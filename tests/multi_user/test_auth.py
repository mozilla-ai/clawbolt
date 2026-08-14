"""Tests for JWT auth and OAuth flow."""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.app.agent.file_store import get_user_store
from backend.app.auth.jwt_auth import (
    create_access_token,
    create_refresh_token,
    decode_access_token,
    decode_refresh_token,
)
from backend.app.auth.oauth_backend import OAuthBackend
from backend.app.auth.oauth_flow import get_or_create_user
from backend.app.models import User


class TestJWTAuth:
    def test_create_and_decode_access_token(self, test_user: User) -> None:
        token = create_access_token(test_user.id)
        payload = decode_access_token(token)
        assert payload["sub"] == str(test_user.id)
        assert payload["type"] == "access"

    def test_create_and_decode_refresh_token(self, test_user: User) -> None:
        token = create_refresh_token(test_user.id)
        payload = decode_refresh_token(token)
        assert payload["sub"] == str(test_user.id)
        assert payload["type"] == "refresh"

    def test_access_token_rejects_refresh(self, test_user: User) -> None:
        token = create_refresh_token(test_user.id)
        with pytest.raises(HTTPException):
            decode_access_token(token)

    def test_refresh_token_rejects_access(self, test_user: User) -> None:
        token = create_access_token(test_user.id)
        with pytest.raises(HTTPException):
            decode_refresh_token(token)

    def test_invalid_token_raises(self) -> None:
        with pytest.raises(HTTPException):
            decode_access_token("invalid.token.here")


class TestOAuthFlow:
    @pytest.mark.asyncio
    async def test_get_or_create_user_new(self, async_db: async_sessionmaker) -> None:
        google_info = {"sub": "new_user_123", "name": "New User"}
        async with async_db() as db:
            user = await get_or_create_user(db, google_info)
        assert user.user_id == "google_new_user_123"

    @pytest.mark.asyncio
    async def test_get_or_create_user_existing(self, async_db: async_sessionmaker) -> None:
        google_info = {"sub": "existing_123", "name": "Existing User"}
        async with async_db() as db:
            u1 = await get_or_create_user(db, google_info)
            u2 = await get_or_create_user(db, google_info)
        assert u1.id == u2.id

    @pytest.mark.asyncio
    async def test_new_oauth_user_gets_default_soul_text(
        self, async_db: async_sessionmaker
    ) -> None:
        """New users created via OAuth should have soul_text seeded by provision_user (#197)."""
        google_info = {"sub": "provision_test_1", "name": "Provision Test"}
        async with async_db() as db:
            user = await get_or_create_user(db, google_info)
            db_user = (
                await db.execute(select(User).where(User.id == user.id))
            ).scalar_one_or_none()
        assert db_user is not None
        assert db_user.soul_text, "soul_text should be non-empty after provision"
        assert db_user.user_text, "user_text should be non-empty after provision"

    @pytest.mark.asyncio
    async def test_existing_oauth_user_skips_provision(self, async_db: async_sessionmaker) -> None:
        """Calling get_or_create_user twice should not re-provision (#197)."""
        google_info = {"sub": "provision_test_2", "name": "Provision Twice"}
        async with async_db() as db:
            u1 = await get_or_create_user(db, google_info)
            db_user = (await db.execute(select(User).where(User.id == u1.id))).scalar_one_or_none()
            assert db_user is not None
            db_user.soul_text = "custom soul"
            await db.commit()

            u2 = await get_or_create_user(db, google_info)
            assert u1.id == u2.id

            db_user = (await db.execute(select(User).where(User.id == u2.id))).scalar_one_or_none()
        assert db_user is not None
        assert db_user.soul_text == "custom soul"


class TestRefreshTokenRevocation:
    @pytest.mark.asyncio
    async def test_deactivated_user_cannot_refresh(
        self, client: TestClient, test_user: User
    ) -> None:
        """Deactivated users must not be able to refresh tokens (regression: #40)."""
        refresh = create_refresh_token(test_user.id)

        # Deactivate the user
        await get_user_store().update(test_user.id, is_active=False)

        resp = client.post("/api/auth/refresh", json={"refresh_token": refresh})
        assert resp.status_code == 401
        assert "deactivated" in resp.json()["detail"].lower()

    def test_active_user_can_refresh(self, client: TestClient, test_user: User) -> None:
        """Active users should be able to refresh tokens normally."""
        refresh = create_refresh_token(test_user.id)
        resp = client.post("/api/auth/refresh", json={"refresh_token": refresh})
        assert resp.status_code == 200
        assert "access_token" in resp.json()


class TestPremiumAuth:
    """``resolve_multi_user`` reads through ``db_session_async()``
    after #429. The user must be created in the same per-test async
    transaction (``async_test_user`` + ``async_db``); a row written via
    the sync ``test_user`` fixture would sit on a different connection
    and be invisible to the async read.
    """

    @pytest.mark.asyncio
    async def test_deactivated_user_returns_401(
        self,
        async_db: async_sessionmaker,
        async_test_user: User,
    ) -> None:
        """Deactivated users must be rejected at auth time (regression: #23)."""
        from unittest.mock import MagicMock

        from starlette.datastructures import State

        from backend.app.auth.session_auth import resolve_multi_user

        # Deactivate the user via the async store so the update lands on
        # the same connection the auth dep reads from.
        await get_user_store().update_async(async_test_user.id, is_active=False)

        token = create_access_token(async_test_user.id)
        request = MagicMock()
        request.state = State()
        request.headers = {"Authorization": f"Bearer {token}"}
        with pytest.raises(HTTPException) as exc_info:
            await resolve_multi_user(request)
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_active_user_authenticates(
        self,
        async_db: async_sessionmaker,
        async_test_user: User,
    ) -> None:
        """Active users should authenticate normally."""
        from unittest.mock import MagicMock

        from starlette.datastructures import State

        from backend.app.auth.session_auth import resolve_multi_user

        token = create_access_token(async_test_user.id)
        request = MagicMock()
        request.state = State()
        request.headers = {"Authorization": f"Bearer {token}"}
        result = await resolve_multi_user(request)
        assert result.id == async_test_user.id


class TestOAuthBackendIsActive:
    """``OAuthBackend.authenticate_login`` reads through ``db_session_async()``
    after the #398 conversion. The user must therefore be created in the
    same per-test async transaction (``async_test_user`` + ``async_db``);
    a row written via the sync ``test_user`` fixture would sit on a
    different connection and be invisible to the async read.
    """

    @pytest.mark.asyncio
    async def test_deactivated_user_rejected_by_backend(
        self,
        async_db: async_sessionmaker,
        async_test_user: User,
    ) -> None:
        """OAuthBackend.authenticate_login must reject deactivated users."""
        await get_user_store().update_async(async_test_user.id, is_active=False)

        backend = OAuthBackend()
        token = create_access_token(async_test_user.id)
        with pytest.raises(HTTPException) as exc_info:
            await backend.authenticate_login({"token": token})
        assert exc_info.value.status_code == 401
        assert "deactivated" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_active_user_accepted_by_backend(
        self,
        async_db: async_sessionmaker,
        async_test_user: User,
    ) -> None:
        """OAuthBackend.authenticate_login should accept active users."""
        backend = OAuthBackend()
        token = create_access_token(async_test_user.id)
        result = await backend.authenticate_login({"token": token})
        assert result.id == async_test_user.id
