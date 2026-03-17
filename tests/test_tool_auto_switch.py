"""Tests for automatic tool group switching based on QuickBooks connection state."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from backend.app.agent.stores import ToolConfigStore
from backend.app.models import User
from backend.app.routers.user_tools import (
    _CORE_FACTORIES,
    _build_tool_list,
    _get_auto_disabled_groups,
)


def _mock_valid_token() -> MagicMock:
    """Create a mock token with valid access_token and refresh_token."""
    token = MagicMock()
    token.access_token = "valid-access-token"
    token.refresh_token = "valid-refresh-token"
    token.expires_at = None
    return token


# ---------------------------------------------------------------------------
# _get_auto_disabled_groups
# ---------------------------------------------------------------------------


def test_auto_disabled_when_qb_connected(test_user: User) -> None:
    """Should return estimate, invoice, email when QB is connected with valid token."""
    with (
        patch(
            "backend.app.routers.user_tools.oauth_service.is_connected",
            return_value=True,
        ),
        patch(
            "backend.app.routers.user_tools.oauth_service.load_token",
            return_value=_mock_valid_token(),
        ),
    ):
        result = _get_auto_disabled_groups(test_user.id)

    assert set(result.keys()) == {"estimate", "invoice", "email"}
    for reason in result.values():
        assert reason == "Managed by QuickBooks"


def test_auto_disabled_when_qb_not_connected(test_user: User) -> None:
    """Should return empty dict when QB is not connected."""
    with patch(
        "backend.app.routers.user_tools.oauth_service.is_connected",
        return_value=False,
    ):
        result = _get_auto_disabled_groups(test_user.id)

    assert result == {}


# ---------------------------------------------------------------------------
# _build_tool_list with auto-disable
# ---------------------------------------------------------------------------


def test_build_tool_list_auto_disabled() -> None:
    """Auto-disabled groups should have enabled=False and auto_disabled_reason set."""
    auto_disabled = {"estimate": "Managed by QuickBooks", "invoice": "Managed by QuickBooks"}
    entries = _build_tool_list(disabled_names=set(), auto_disabled=auto_disabled)

    for entry in entries:
        if entry.name in auto_disabled:
            assert entry.enabled is False, f"{entry.name} should be disabled"
            assert entry.auto_disabled_reason == "Managed by QuickBooks"
        elif entry.name in _CORE_FACTORIES:
            assert entry.enabled is True
            assert entry.auto_disabled_reason is None


def test_build_tool_list_no_auto_disabled() -> None:
    """Without auto-disable, tools should follow normal disabled_names logic."""
    entries = _build_tool_list(disabled_names={"estimate"}, auto_disabled={})

    for entry in entries:
        if entry.name == "estimate":
            assert entry.enabled is False
            assert entry.auto_disabled_reason is None
        elif entry.name in _CORE_FACTORIES:
            assert entry.enabled is True


def test_build_tool_list_auto_overrides_user_enabled() -> None:
    """Auto-disable should override even if the user hasn't disabled the group."""
    auto_disabled = {"email": "Managed by QuickBooks"}
    entries = _build_tool_list(disabled_names=set(), auto_disabled=auto_disabled)

    email_entry = next((e for e in entries if e.name == "email"), None)
    assert email_entry is not None
    assert email_entry.enabled is False
    assert email_entry.auto_disabled_reason == "Managed by QuickBooks"


def test_build_tool_list_core_not_auto_disabled() -> None:
    """Core tools should never be auto-disabled even if listed."""
    auto_disabled = {"messaging": "Should not work"}
    entries = _build_tool_list(disabled_names=set(), auto_disabled=auto_disabled)

    msg_entry = next((e for e in entries if e.name == "messaging"), None)
    assert msg_entry is not None
    assert msg_entry.enabled is True
    assert msg_entry.auto_disabled_reason is None


# ---------------------------------------------------------------------------
# Router integration: disabled_groups includes auto-disabled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_agent_router_auto_disables_when_qb_connected(test_user: User) -> None:
    """The agent router should add auto-disabled groups to excluded_factories."""
    store = ToolConfigStore(test_user.id)
    disabled = await store.get_disabled_tool_names()

    # Simulate QB connected
    with patch(
        "backend.app.services.oauth.oauth_service.is_connected",
        return_value=True,
    ):
        from backend.app.services.oauth import oauth_service

        if oauth_service.is_connected(test_user.id, "quickbooks"):
            auto_disabled = {"estimate", "invoice", "email"}
            combined = (disabled or set()) | auto_disabled
        else:
            combined = disabled or set()

    assert "estimate" in combined
    assert "invoice" in combined
    assert "email" in combined


@pytest.mark.asyncio()
async def test_agent_router_no_auto_disable_when_qb_disconnected(test_user: User) -> None:
    """The agent router should not add auto-disabled groups when QB is disconnected."""
    store = ToolConfigStore(test_user.id)
    disabled = await store.get_disabled_tool_names()

    with patch(
        "backend.app.services.oauth.oauth_service.is_connected",
        return_value=False,
    ):
        from backend.app.services.oauth import oauth_service

        if oauth_service.is_connected(test_user.id, "quickbooks"):
            auto_disabled = {"estimate", "invoice", "email"}
            combined = (disabled or set()) | auto_disabled
        else:
            combined = disabled or set()

    assert "estimate" not in combined
    assert "invoice" not in combined
    assert "email" not in combined


# ---------------------------------------------------------------------------
# API endpoint integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_get_tool_config_shows_auto_disabled(
    test_user: User,
) -> None:
    """GET /user/tools should include auto_disabled_reason when QB is connected."""
    from unittest.mock import AsyncMock

    from fastapi.testclient import TestClient

    from backend.app.auth.dependencies import get_current_user
    from backend.app.main import app

    def _override() -> User:
        return test_user

    app.dependency_overrides[get_current_user] = _override
    try:
        with (
            patch("backend.app.main._verify_llm_settings", new_callable=AsyncMock),
            patch("backend.app.agent.heartbeat.heartbeat_scheduler.start"),
            patch(
                "backend.app.routers.user_tools.oauth_service.is_connected",
                return_value=True,
            ),
            patch(
                "backend.app.routers.user_tools.oauth_service.load_token",
                return_value=_mock_valid_token(),
            ),
        ):
            client = TestClient(app)
            resp = client.get("/api/user/tools")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    tools = resp.json()["tools"]

    estimate_tool = next((t for t in tools if t["name"] == "estimate"), None)
    assert estimate_tool is not None
    assert estimate_tool["enabled"] is False
    assert estimate_tool["auto_disabled_reason"] == "Managed by QuickBooks"

    # Core tools should not be affected
    messaging_tool = next((t for t in tools if t["name"] == "messaging"), None)
    assert messaging_tool is not None
    assert messaging_tool["enabled"] is True
    assert messaging_tool["auto_disabled_reason"] is None


@pytest.mark.asyncio()
async def test_get_tool_config_no_auto_disabled_when_disconnected(
    test_user: User,
) -> None:
    """GET /user/tools should not auto-disable when QB is not connected."""
    from unittest.mock import AsyncMock

    from fastapi.testclient import TestClient

    from backend.app.auth.dependencies import get_current_user
    from backend.app.main import app

    def _override() -> User:
        return test_user

    app.dependency_overrides[get_current_user] = _override
    try:
        with (
            patch("backend.app.main._verify_llm_settings", new_callable=AsyncMock),
            patch("backend.app.agent.heartbeat.heartbeat_scheduler.start"),
            patch(
                "backend.app.routers.user_tools.oauth_service.is_connected",
                return_value=False,
            ),
        ):
            client = TestClient(app)
            resp = client.get("/api/user/tools")
    finally:
        app.dependency_overrides.clear()

    assert resp.status_code == 200
    tools = resp.json()["tools"]

    estimate_tool = next((t for t in tools if t["name"] == "estimate"), None)
    assert estimate_tool is not None
    assert estimate_tool["auto_disabled_reason"] is None
