"""Tests for the ServiceTitan factory + connect tool.

Verifies the split-factory wiring (auth tools always reachable, data
factory gated on credential state) and the end-to-end connect flow
against the in-process fake.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

import backend.app.integrations.servicetitan.factory as factory_module
from backend.app.agent.tools.names import ToolName
from backend.app.agent.tools.registry import ToolContext, default_registry
from backend.app.integrations.servicetitan import _fake as fake_module
from backend.app.integrations.servicetitan.auth import (
    is_connected,
    load_credentials,
    save_credentials,
)
from backend.app.integrations.servicetitan.auth_tools import build_auth_tools


@pytest.fixture(autouse=True)
def _force_fake_backend_and_app_key() -> Any:
    """Pin every test to the fake backend and supply a real-looking App Key."""
    from backend.app.config import settings as _settings

    with (
        patch.object(_settings, "servicetitan_use_fake", True),
        patch.object(_settings, "servicetitan_app_key", "fake-st-app-key"),
    ):
        fake_module.reset_default_fake_backend()
        try:
            yield
        finally:
            fake_module.reset_default_fake_backend()


def _make_context(user_id: str) -> ToolContext:
    """Build a minimal ToolContext for factory invocation.

    The registry's ``ToolContext`` carries a User object; for these
    tests only ``user.id`` is read so a MagicMock with the right
    attribute is enough.
    """
    user = MagicMock()
    user.id = user_id
    ctx = MagicMock(spec=ToolContext)
    ctx.user = user
    return ctx


# ---------------------------------------------------------------------------
# Registry wiring
# ---------------------------------------------------------------------------


def test_factories_registered() -> None:
    """Both the auth and data factories must be discoverable by name."""
    assert "servicetitan_auth" in default_registry.factory_names
    assert "servicetitan" in default_registry.factory_names


def test_auth_factory_is_core_data_factory_is_specialist() -> None:
    """Split-factory invariants: connect tool always on schema, data gated."""
    auth = default_registry.get_factory("servicetitan_auth")
    data = default_registry.get_factory("servicetitan")
    assert auth is not None
    assert data is not None
    assert auth.core is True, "connect_servicetitan must stay on the schema"
    assert data.core is False, "data tools must be specialist (gated)"
    assert data.auth_check is not None, "data factory needs an auth_check"


def test_auth_factory_lists_connect_subtool() -> None:
    auth = default_registry.get_factory("servicetitan_auth")
    assert auth is not None
    names = [s.name for s in auth.sub_tools]
    assert ToolName.SERVICETITAN_CONNECT in names


# ---------------------------------------------------------------------------
# auth_check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_auth_check_returns_reason_when_not_connected(
    async_test_user: Any,
) -> None:
    ctx = _make_context(async_test_user.id)
    reason = await factory_module._servicetitan_auth_check(ctx)
    assert reason is not None
    assert "ServiceTitan is not connected" in reason


@pytest.mark.asyncio()
async def test_auth_check_returns_none_when_connected(async_test_user: Any) -> None:
    user_id = async_test_user.id
    await save_credentials(
        user_id,
        tenant_id=str(fake_module.DEFAULT_TENANT_ID),
        client_id="cid",
        client_secret="csec",
        app_key="fake-st-app-key",
        access_token=fake_module.FAKE_TOKEN_VALUE,
        expires_at=time.time() + 600,
    )
    ctx = _make_context(user_id)
    assert await factory_module._servicetitan_auth_check(ctx) is None


# ---------------------------------------------------------------------------
# Data factory body
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_data_factory_returns_empty_when_not_connected(
    async_test_user: Any,
) -> None:
    ctx = _make_context(async_test_user.id)
    tools = await factory_module._servicetitan_factory(ctx)
    assert tools == []


@pytest.mark.asyncio()
async def test_data_factory_returns_empty_when_connected(async_test_user: Any) -> None:
    """No data tools wired yet; the scaffold's contract is an empty list."""
    user_id = async_test_user.id
    await save_credentials(
        user_id,
        tenant_id=str(fake_module.DEFAULT_TENANT_ID),
        client_id="cid",
        client_secret="csec",
        app_key="fake-st-app-key",
        access_token=fake_module.FAKE_TOKEN_VALUE,
        expires_at=time.time() + 600,
    )
    ctx = _make_context(user_id)
    tools = await factory_module._servicetitan_factory(ctx)
    assert tools == []


# ---------------------------------------------------------------------------
# connect_servicetitan tool: end-to-end against the fake
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_connect_servicetitan_persists_credentials(async_test_user: Any) -> None:
    user_id = async_test_user.id
    tools = build_auth_tools(user_id)
    assert len(tools) == 1
    connect_tool = tools[0]
    assert connect_tool.name == ToolName.SERVICETITAN_CONNECT

    result = await connect_tool.function(
        tenant_id="1234567",
        client_id="my-client-id",
        client_secret="my-secret",
    )
    assert result.is_error is False
    assert "connected" in result.content.lower()
    assert result.receipt is not None
    assert "1234567" in result.receipt.target

    assert await is_connected(user_id) is True
    cred = await load_credentials(user_id)
    assert cred is not None
    assert cred.tenant_id == "1234567"
    assert cred.client_id == "my-client-id"
    assert cred.client_secret == "my-secret"
    assert cred.app_key == "fake-st-app-key"
    assert cred.access_token == fake_module.FAKE_TOKEN_VALUE
    assert cred.expires_at > time.time()


@pytest.mark.asyncio()
async def test_connect_servicetitan_rejects_empty_fields(async_test_user: Any) -> None:
    user_id = async_test_user.id
    tools = build_auth_tools(user_id)
    connect_tool = tools[0]

    result = await connect_tool.function(
        tenant_id="",
        client_id="cid",
        client_secret="csec",
    )
    assert result.is_error is True
    assert result.error_kind is not None
    assert "required" in result.content.lower()


@pytest.mark.asyncio()
async def test_connect_servicetitan_errors_without_app_key(
    async_test_user: Any,
) -> None:
    """When the operator has not set the App Key, connect must fail loudly."""
    from backend.app.config import settings as _settings

    user_id = async_test_user.id
    with patch.object(_settings, "servicetitan_app_key", ""):
        tools = build_auth_tools(user_id)
        connect_tool = tools[0]
        result = await connect_tool.function(
            tenant_id="t",
            client_id="cid",
            client_secret="csec",
        )
    assert result.is_error is True
    assert "SERVICETITAN_APP_KEY" in result.content


@pytest.mark.asyncio()
async def test_connect_servicetitan_strips_and_rejects_blank_client_secret(
    async_test_user: Any,
) -> None:
    """A whitespace-only Client Secret must surface as a validation error."""
    user_id = async_test_user.id
    tools = build_auth_tools(user_id)
    connect_tool = tools[0]

    result = await connect_tool.function(
        tenant_id="t",
        client_id="cid",
        client_secret="   ",  # stripped to empty before the mint call
    )
    assert result.is_error is True
    assert "required" in result.content.lower()


@pytest.mark.asyncio()
async def test_connect_servicetitan_surfaces_token_endpoint_failure(
    async_test_user: Any,
) -> None:
    """When the fake rejects the credentials, the tool surfaces an AUTH error.

    Patches the mint helper to raise so this test actually exercises the
    failure-path branch in the connect tool, not the local validation
    branch that catches blank fields up front.
    """
    user_id = async_test_user.id
    tools = build_auth_tools(user_id)
    connect_tool = tools[0]

    from backend.app.integrations.servicetitan.auth import ServiceTitanAuthError

    async def _fail(**kwargs: Any) -> tuple[str, float]:
        raise ServiceTitanAuthError("ServiceTitan rejected the client credentials (HTTP 401)")

    with patch(
        "backend.app.integrations.servicetitan.auth_tools.mint_access_token",
        side_effect=_fail,
    ):
        result = await connect_tool.function(
            tenant_id="1234567",
            client_id="cid",
            client_secret="csec",
        )
    assert result.is_error is True
    assert "rejected" in result.content.lower()
    assert result.error_kind is not None
