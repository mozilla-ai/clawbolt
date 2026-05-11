"""Tests for the ServiceTitan HTTP service layer.

Exercises header injection, refresh-on-401, and the fake-transport
plumbing. Resource-specific helpers (list customers, get job, etc.)
land in the read-tools issue (#1300); these tests cover only the
generic ``request`` surface this scaffold ships.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import patch

import pytest

from backend.app.integrations.servicetitan import _fake as fake_module
from backend.app.integrations.servicetitan.auth import (
    clear_credentials,
    load_credentials,
    save_credentials,
)
from backend.app.integrations.servicetitan.service import (
    ServiceTitanError,
    ServiceTitanNotConnectedError,
    ServiceTitanService,
    build_service_for_user,
)


async def _load(user_id: str) -> Any:
    cred = await load_credentials(user_id)
    assert cred is not None
    return cred


@pytest.fixture(autouse=True)
def _force_fake_backend() -> Any:
    """Pin every test in this module to the in-process fake backend."""
    from backend.app.config import settings as _settings

    with patch.object(_settings, "servicetitan_use_fake", True):
        fake_module.reset_default_fake_backend()
        try:
            yield
        finally:
            fake_module.reset_default_fake_backend()


# ---------------------------------------------------------------------------
# build_service_for_user
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_build_service_returns_none_without_credentials(
    async_test_user: Any,
) -> None:
    assert await build_service_for_user(async_test_user.id) is None


@pytest.mark.asyncio()
async def test_build_service_eagerly_refreshes_bearer(async_test_user: Any) -> None:
    """An expired row at build time should still produce a usable service."""
    user_id = async_test_user.id
    await save_credentials(
        user_id,
        tenant_id=str(fake_module.DEFAULT_TENANT_ID),
        client_id="cid",
        client_secret="csec",
        app_key="fake-st-app-key",
        access_token="stale",
        expires_at=time.time() - 60,
    )
    service = await build_service_for_user(user_id)
    assert service is not None
    assert service.credential.access_token == fake_module.FAKE_TOKEN_VALUE


# ---------------------------------------------------------------------------
# Request loop: headers, refresh-on-401, errors
# ---------------------------------------------------------------------------


def _seed_connected_credential() -> dict[str, Any]:
    """Return the credential fields a connected user would hold.

    The fake backend issues ``FAKE_TOKEN_VALUE`` and gates resource
    endpoints on ``ST-App-Key=fake-st-app-key``. Match both so the
    service routes through the happy path without a refresh.
    """
    return {
        "tenant_id": str(fake_module.DEFAULT_TENANT_ID),
        "client_id": "cid",
        "client_secret": "csec",
        "app_key": "fake-st-app-key",
        "access_token": fake_module.FAKE_TOKEN_VALUE,
        "expires_at": time.time() + 600,
    }


@pytest.mark.asyncio()
async def test_request_sends_bearer_and_app_key_headers(async_test_user: Any) -> None:
    user_id = async_test_user.id
    await save_credentials(user_id, **_seed_connected_credential())
    service = await build_service_for_user(user_id)
    assert service is not None

    # Hit a list endpoint the fake serves directly. Successful round
    # trip proves both headers were sent (the fake 401s otherwise).
    path = f"/crm/v2/tenant/{service.tenant_id}/customers"
    payload = await service.get(path)
    assert isinstance(payload, dict)
    assert "data" in payload
    assert payload["totalCount"] == 10  # seed customer count


@pytest.mark.asyncio()
async def test_request_refreshes_bearer_on_401(async_test_user: Any) -> None:
    """A stale bearer that the fake rejects should be refreshed once."""
    user_id = async_test_user.id
    creds = _seed_connected_credential()
    # Force a value the fake will reject so the service is pushed onto
    # the refresh path.
    creds["access_token"] = "stale-bearer-not-recognized"
    await save_credentials(user_id, **creds)

    service = ServiceTitanService(
        user_id,
        # Build a credential snapshot that matches what was just persisted
        # so the in-process service starts the request loop with the
        # rejected bearer rather than triggering the eager refresh in
        # ``build_service_for_user``.
        credential=(await _load(user_id)),
    )

    path = f"/crm/v2/tenant/{service.tenant_id}/customers"
    payload = await service.get(path)
    assert isinstance(payload, dict)
    assert payload["totalCount"] == 10
    # The refresh path swapped in the fake's mint result.
    assert service.credential.access_token == fake_module.FAKE_TOKEN_VALUE


@pytest.mark.asyncio()
async def test_request_raises_for_unknown_path(async_test_user: Any) -> None:
    user_id = async_test_user.id
    await save_credentials(user_id, **_seed_connected_credential())
    service = await build_service_for_user(user_id)
    assert service is not None

    with pytest.raises(ServiceTitanError):
        await service.get("/this/does/not/exist")


@pytest.mark.asyncio()
async def test_request_without_usable_credential_raises(async_test_user: Any) -> None:
    """A service built from an empty bearer that cannot be refreshed should fail loudly."""
    user_id = async_test_user.id
    # Persist a credential, then clear it so the refresh path returns
    # None when the service tries to mint a token.
    await save_credentials(user_id, **_seed_connected_credential())
    cred = await _load(user_id)

    # Wipe the bearer in-memory and delete the row so refresh returns None.
    cred.access_token = ""
    cred.expires_at = 0.0
    await clear_credentials(user_id)

    service = ServiceTitanService(user_id, cred)
    with pytest.raises(ServiceTitanNotConnectedError):
        await service.get(f"/crm/v2/tenant/{service.tenant_id}/customers")


@pytest.mark.asyncio()
async def test_one_post_to_job_notes_round_trips(async_test_user: Any) -> None:
    """Smoke test: a POST also threads through the auth headers and fake."""
    user_id = async_test_user.id
    await save_credentials(user_id, **_seed_connected_credential())
    service = await build_service_for_user(user_id)
    assert service is not None

    seed_job_id = next(iter(fake_module.iter_seed_job_ids()))
    path = f"/jpm/v2/tenant/{service.tenant_id}/jobs/{seed_job_id}/notes"
    note = await service.post(path, json_body={"text": "scaffold smoke test"})
    assert isinstance(note, dict)
    assert note["text"] == "scaffold smoke test"
