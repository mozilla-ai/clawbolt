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


def test_data_factory_lists_subtools_with_expected_defaults() -> None:
    """The data factory must advertise the read tools (default ALWAYS) and
    the write tool ``st_add_job_note`` (default ASK), so the Settings UI
    and ``manage_integration`` render the right permission rows even
    before the user connects.
    """
    data = default_registry.get_factory("servicetitan")
    assert data is not None
    names = {s.name for s in data.sub_tools}
    assert ToolName.SERVICETITAN_SEARCH_CUSTOMERS in names
    assert ToolName.SERVICETITAN_GET_CUSTOMER in names
    assert ToolName.SERVICETITAN_LIST_APPOINTMENTS in names
    assert ToolName.SERVICETITAN_ADD_JOB_NOTE in names

    expected_defaults = {
        ToolName.SERVICETITAN_SEARCH_CUSTOMERS: "always",
        ToolName.SERVICETITAN_GET_CUSTOMER: "always",
        ToolName.SERVICETITAN_LIST_APPOINTMENTS: "always",
        ToolName.SERVICETITAN_ADD_JOB_NOTE: "ask",
    }
    for sub in data.sub_tools:
        assert sub.default_permission == expected_defaults[sub.name], (
            f"{sub.name} default_permission should be {expected_defaults[sub.name]}"
        )


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
async def test_data_factory_returns_all_tools_when_connected(async_test_user: Any) -> None:
    """A connected user gets the read tools plus the write tool."""
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
    names = {t.name for t in tools}
    assert names == {
        ToolName.SERVICETITAN_SEARCH_CUSTOMERS,
        ToolName.SERVICETITAN_GET_CUSTOMER,
        ToolName.SERVICETITAN_LIST_APPOINTMENTS,
        ToolName.SERVICETITAN_ADD_JOB_NOTE,
    }


# ---------------------------------------------------------------------------
# connect_servicetitan tool: end-to-end against the fake
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_connect_servicetitan_redirects_to_web_app(async_test_user: Any) -> None:
    """The auth tool now redirects to the Clawbolt web app instead of
    accepting credentials in chat. Issue #1337."""
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
    assert result.is_error is True
    assert "Clawbolt web app" in result.content
    assert "Settings > Tools" in result.content
    assert "ServiceTitan" in result.content

    # No credentials should have been persisted.
    assert await is_connected(user_id) is False


@pytest.mark.asyncio()
async def test_connect_servicetitan_redirects_even_with_empty_fields(
    async_test_user: Any,
) -> None:
    """The tool now always redirects to web app regardless of input."""
    user_id = async_test_user.id
    tools = build_auth_tools(user_id)
    connect_tool = tools[0]

    result = await connect_tool.function(
        tenant_id="",
        client_id="cid",
        client_secret="csec",
    )
    assert result.is_error is True
    assert "Clawbolt web app" in result.content


@pytest.mark.asyncio()
async def test_connect_servicetitan_redirects_regardless_of_app_key(
    async_test_user: Any,
) -> None:
    """The tool now always redirects to web app regardless of App Key config."""
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
    assert "Clawbolt web app" in result.content


@pytest.mark.asyncio()
async def test_connect_servicetitan_redirects_even_with_blank_secret(
    async_test_user: Any,
) -> None:
    """The tool now always redirects to web app regardless of input."""
    user_id = async_test_user.id
    tools = build_auth_tools(user_id)
    connect_tool = tools[0]

    result = await connect_tool.function(
        tenant_id="t",
        client_id="cid",
        client_secret="   ",
    )
    assert result.is_error is True
    assert "Clawbolt web app" in result.content


@pytest.mark.asyncio()
async def test_connect_servicetitan_redirects_regardless_of_token_failure(
    async_test_user: Any,
) -> None:
    """The tool now always redirects to web app regardless of token state."""
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
    assert "Clawbolt web app" in result.content
