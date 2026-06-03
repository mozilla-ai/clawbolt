"""Tests for the web-form integration connect endpoints.

ServiceTitan and AppFolio authenticate with pasted secrets. These secrets
are submitted through these endpoints over an authenticated web session
instead of a chat thread (issue #1337). The endpoints reuse the same
``connect_credentials`` / ``connect_via_magic_link`` orchestration the chat
tools used before they were removed.
"""

from __future__ import annotations

from collections.abc import Generator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from backend.app.auth.dependencies import get_current_user
from backend.app.database import db_session_async
from backend.app.integrations.appfolio_vendor.service import AppFolioError
from backend.app.integrations.servicetitan.auth import ServiceTitanAuthError
from backend.app.main import app
from backend.app.models import User


@pytest.fixture()
async def test_user() -> User:
    async with db_session_async() as db:
        user = User(user_id="integrations-test-user", onboarding_complete=True)
        db.add(user)
        await db.commit()
        await db.refresh(user)
        db.expunge(user)
    return user


@pytest.fixture()
def client(test_user: User) -> Generator[TestClient]:
    def _override() -> User:
        return test_user

    app.dependency_overrides[get_current_user] = _override
    with (
        patch("backend.app.main._verify_llm_settings", new_callable=AsyncMock),
        patch("backend.app.agent.heartbeat.heartbeat_scheduler.start"),
        patch("backend.app.channels.telegram.settings.telegram_allowed_chat_id", "*"),
        patch("backend.app.channels.telegram.settings.telegram_bot_token", ""),
        patch("backend.app.agent.ingestion.settings.message_batch_window_ms", 0),
        TestClient(app) as c,
    ):
        yield c
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# ServiceTitan
# ---------------------------------------------------------------------------


def test_servicetitan_connect_persists(client: TestClient) -> None:
    save_mock = AsyncMock()
    with patch(
        "backend.app.routers.integrations.servicetitan_auth.connect_credentials",
        new=save_mock,
    ):
        resp = client.post(
            "/api/integrations/servicetitan/connect",
            json={"tenant_id": "t1", "client_id": "cid", "client_secret": "csec"},
        )
    assert resp.status_code == 200
    assert resp.json() == {"integration": "servicetitan", "connected": True}
    save_mock.assert_awaited_once()


def test_servicetitan_connect_rejects_bad_credentials(client: TestClient) -> None:
    async def _fail(*args: Any, **kwargs: Any) -> None:
        raise ServiceTitanAuthError("ServiceTitan rejected the client credentials (HTTP 401)")

    with patch(
        "backend.app.routers.integrations.servicetitan_auth.connect_credentials",
        new=_fail,
    ):
        resp = client.post(
            "/api/integrations/servicetitan/connect",
            json={"tenant_id": "t1", "client_id": "cid", "client_secret": "wrong"},
        )
    assert resp.status_code == 400
    assert "rejected" in resp.json()["detail"].lower()


def test_servicetitan_connect_validates_empty_fields(client: TestClient) -> None:
    """Pydantic rejects a blank field before the orchestration runs."""
    resp = client.post(
        "/api/integrations/servicetitan/connect",
        json={"tenant_id": "", "client_id": "cid", "client_secret": "csec"},
    )
    assert resp.status_code == 422


def test_servicetitan_disconnect(client: TestClient) -> None:
    with (
        patch(
            "backend.app.routers.integrations.servicetitan_auth.is_connected",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "backend.app.routers.integrations.servicetitan_auth.clear_credentials",
            new=AsyncMock(),
        ) as clear_mock,
    ):
        resp = client.delete("/api/integrations/servicetitan")
    assert resp.status_code == 200
    assert resp.json() == {"integration": "servicetitan", "connected": False}
    clear_mock.assert_awaited_once()


def test_servicetitan_disconnect_when_not_connected(client: TestClient) -> None:
    with patch(
        "backend.app.routers.integrations.servicetitan_auth.is_connected",
        new=AsyncMock(return_value=False),
    ):
        resp = client.delete("/api/integrations/servicetitan")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# AppFolio
# ---------------------------------------------------------------------------


def test_appfolio_connect_persists(client: TestClient) -> None:
    with patch(
        "backend.app.routers.integrations.connect_via_magic_link",
        new=AsyncMock(),
    ) as connect_mock:
        resp = client.post(
            "/api/integrations/appfolio_vendor/connect",
            json={"magic_link": "https://vendor.appfolio.com/?magic_link_token=eyJ.fake"},
        )
    assert resp.status_code == 200
    assert resp.json() == {"integration": "appfolio_vendor", "connected": True}
    connect_mock.assert_awaited_once()


def test_appfolio_connect_rejects_bad_link(client: TestClient) -> None:
    async def _fail(*args: Any, **kwargs: Any) -> None:
        raise AppFolioError("AppFolio OAuth exchange failed: HTTP 400")

    with patch(
        "backend.app.routers.integrations.connect_via_magic_link",
        new=_fail,
    ):
        resp = client.post(
            "/api/integrations/appfolio_vendor/connect",
            json={"magic_link": "expired-token"},
        )
    assert resp.status_code == 400
    assert "appfolio" in resp.json()["detail"].lower()


def test_appfolio_disconnect(client: TestClient) -> None:
    with (
        patch(
            "backend.app.routers.integrations.appfolio_auth.is_connected",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "backend.app.routers.integrations.appfolio_auth.clear_credential",
            new=AsyncMock(),
        ) as clear_mock,
    ):
        resp = client.delete("/api/integrations/appfolio_vendor")
    assert resp.status_code == 200
    assert resp.json() == {"integration": "appfolio_vendor", "connected": False}
    clear_mock.assert_awaited_once()
