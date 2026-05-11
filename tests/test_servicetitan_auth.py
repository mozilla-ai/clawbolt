"""Tests for the ServiceTitan auth + credential layer.

Exercises the paste-credentials -> token-mint -> persist round trip
against the in-process fake backend, plus the refresh path that swaps
in a fresh bearer when the stored one expires.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from backend.app.integrations.servicetitan import _fake as fake_module
from backend.app.integrations.servicetitan.auth import (
    INTEGRATION_NAME,
    ServiceTitanAuthError,
    ServiceTitanCredential,
    clear_credentials,
    get_valid_token,
    is_connected,
    load_credentials,
    mint_access_token,
    save_credentials,
)
from backend.app.services.oauth import oauth_service


@pytest.fixture(autouse=True)
def _force_fake_backend() -> Any:
    """Pin every test in this module to the in-process fake backend.

    The fake's MockTransport is built lazily inside the auth/service
    modules from ``settings.servicetitan_use_fake``; the tests assume
    that flag is true. They also reset the singleton fake between tests
    so a mutation in one case (e.g. ``expire_all_tokens``) does not
    bleed into another.
    """
    from backend.app.config import settings as _settings

    with patch.object(_settings, "servicetitan_use_fake", True):
        fake_module.reset_default_fake_backend()
        try:
            yield
        finally:
            fake_module.reset_default_fake_backend()


# ---------------------------------------------------------------------------
# Token minting (transport-level)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_mint_access_token_round_trips_against_fake() -> None:
    """The fake's ``/connect/token`` accepts any non-empty client credentials."""
    token, expires_at = await mint_access_token(
        client_id="abc123",
        client_secret="shhh",
    )
    assert token == fake_module.FAKE_TOKEN_VALUE
    # The fake honors ``expires_in=900``; expires_at should land roughly
    # 15 minutes out without claiming a value in the past.
    assert expires_at > time.time() + 60


@pytest.mark.asyncio()
async def test_mint_access_token_rejects_missing_client_secret() -> None:
    """An empty client_secret should round-trip through to a 4xx error."""
    with pytest.raises(ServiceTitanAuthError):
        await mint_access_token(client_id="abc", client_secret="")


@pytest.mark.asyncio()
async def test_mint_access_token_wraps_network_failure() -> None:
    """A transport-level failure surfaces as ServiceTitanAuthError, not httpx."""

    def _blow_up(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated DNS failure")

    bad_transport = httpx.MockTransport(_blow_up)
    # ``auth.py`` imports ``build_fake_transport`` into its own module
    # namespace at import time, so the patch must target that namespace
    # rather than the source module.
    with (
        patch(
            "backend.app.integrations.servicetitan.auth.build_fake_transport",
            lambda backend=None: bad_transport,
        ),
        pytest.raises(ServiceTitanAuthError),
    ):
        await mint_access_token(client_id="abc", client_secret="xyz")


# ---------------------------------------------------------------------------
# Credential persistence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_save_and_load_credentials_round_trip(async_test_user: Any) -> None:
    user_id = async_test_user.id

    assert await load_credentials(user_id) is None
    assert await is_connected(user_id) is False

    await save_credentials(
        user_id,
        tenant_id="tenant-001",
        client_id="cid-001",
        client_secret="csec-001",
        app_key="app-key-001",
        access_token="bearer-001",
        expires_at=time.time() + 600,
    )

    cred = await load_credentials(user_id)
    assert cred is not None
    assert cred.tenant_id == "tenant-001"
    assert cred.client_id == "cid-001"
    assert cred.client_secret == "csec-001"
    assert cred.app_key == "app-key-001"
    assert cred.access_token == "bearer-001"
    assert cred.expires_at > time.time()
    assert await is_connected(user_id) is True


@pytest.mark.asyncio()
async def test_save_credentials_overwrites_previous_row(async_test_user: Any) -> None:
    """A second save replaces tenant + bearer cleanly."""
    user_id = async_test_user.id
    await save_credentials(
        user_id,
        tenant_id="tenant-A",
        client_id="cid-A",
        client_secret="csec-A",
        app_key="app-key",
        access_token="bearer-A",
        expires_at=time.time() + 600,
    )
    await save_credentials(
        user_id,
        tenant_id="tenant-B",
        client_id="cid-B",
        client_secret="csec-B",
        app_key="app-key",
        access_token="bearer-B",
        expires_at=time.time() + 600,
    )

    cred = await load_credentials(user_id)
    assert cred is not None
    assert cred.tenant_id == "tenant-B"
    assert cred.client_id == "cid-B"
    assert cred.access_token == "bearer-B"


@pytest.mark.asyncio()
async def test_clear_credentials_removes_row(async_test_user: Any) -> None:
    user_id = async_test_user.id
    await save_credentials(
        user_id,
        tenant_id="tenant-1",
        client_id="cid",
        client_secret="csec",
        app_key="app-key",
        access_token="bearer",
        expires_at=time.time() + 600,
    )
    assert await is_connected(user_id) is True
    await clear_credentials(user_id)
    assert await is_connected(user_id) is False
    assert await load_credentials(user_id) is None


@pytest.mark.asyncio()
async def test_load_credentials_treats_partial_row_as_not_connected(
    async_test_user: Any,
) -> None:
    """A row without tenant_id / client_id / client_secret is not usable.

    Defends against a half-built row from a connect tool that crashed
    after saving an empty placeholder.
    """
    user_id = async_test_user.id
    from backend.app.services.oauth import OAuthTokenData

    # Persist a token row with empty extra metadata.
    token = OAuthTokenData(
        access_token="",
        refresh_token="",
        token_type="Bearer",
        expires_at=0.0,
        scopes=[],
        realm_id="",
        extra={},
    )
    await oauth_service.save_token(user_id, INTEGRATION_NAME, token)

    assert await load_credentials(user_id) is None
    assert await is_connected(user_id) is False


# ---------------------------------------------------------------------------
# get_valid_token: lazy refresh
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_credential_token_expiry_window() -> None:
    """``is_token_expired`` respects the buffer and the empty-token sentinel."""
    cred = ServiceTitanCredential(
        tenant_id="t",
        client_id="c",
        client_secret="s",
        app_key="k",
        access_token="",
        expires_at=0.0,
    )
    assert cred.is_token_expired(now=1000.0) is True

    cred.access_token = "tok"
    cred.expires_at = 1000.0
    assert cred.is_token_expired(now=2000.0) is True
    assert cred.is_token_expired(now=500.0) is False
    # Inside the safety buffer: treat as expired so the bearer never
    # lapses mid-call.
    assert cred.is_token_expired(now=990.0) is True


@pytest.mark.asyncio()
async def test_get_valid_token_returns_none_when_no_credential(
    async_test_user: Any,
) -> None:
    assert await get_valid_token(async_test_user.id) is None


@pytest.mark.asyncio()
async def test_get_valid_token_reuses_fresh_bearer(async_test_user: Any) -> None:
    """A non-expired bearer is returned without a refresh call."""
    user_id = async_test_user.id
    await save_credentials(
        user_id,
        tenant_id="t1",
        client_id="cid",
        client_secret="csec",
        app_key="app-key",
        access_token="still-fresh",
        expires_at=time.time() + 3600,
    )

    async def _explode(**kwargs: Any) -> tuple[str, float]:
        raise AssertionError("mint should not be called when token is fresh")

    with patch(
        "backend.app.integrations.servicetitan.auth.mint_access_token",
        side_effect=_explode,
    ):
        cred = await get_valid_token(user_id)
    assert cred is not None
    assert cred.access_token == "still-fresh"


@pytest.mark.asyncio()
async def test_get_valid_token_refreshes_when_expired(async_test_user: Any) -> None:
    """An expired bearer triggers a fresh client-credentials mint."""
    user_id = async_test_user.id
    await save_credentials(
        user_id,
        tenant_id="t1",
        client_id="cid",
        client_secret="csec",
        app_key="app-key",
        access_token="stale-token",
        expires_at=time.time() - 10,
    )

    cred = await get_valid_token(user_id)
    assert cred is not None
    # The fake mints its constant token value; the helper persists it
    # back to the row, so reading the row again yields the fresh bearer.
    assert cred.access_token == fake_module.FAKE_TOKEN_VALUE
    assert cred.expires_at > time.time()

    reloaded = await load_credentials(user_id)
    assert reloaded is not None
    assert reloaded.access_token == fake_module.FAKE_TOKEN_VALUE
