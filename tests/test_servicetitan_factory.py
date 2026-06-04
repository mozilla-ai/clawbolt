"""Tests for the ServiceTitan factory + connect flow.

Verifies the data factory is gated on credential state and that the
``connect_credentials`` orchestration (used by the web connect endpoint)
validates and persists end to end against the in-process fake. Connecting
moved out of chat into the web app, so there is no longer a connect tool on
the agent schema (issue #1337).
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
    ServiceTitanAuthError,
    connect_credentials,
    is_connected,
    load_credentials,
    save_credentials,
)


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


def test_data_factory_registered_and_no_chat_connect_factory() -> None:
    """The data factory is discoverable; the old chat connect factory is gone."""
    assert "servicetitan" in default_registry.factory_names
    assert "servicetitan_auth" not in default_registry.factory_names


def test_data_factory_is_specialist_with_auth_check() -> None:
    """Data tools must be specialist (gated) with an auth_check."""
    data = default_registry.get_factory("servicetitan")
    assert data is not None
    assert data.core is False, "data tools must be specialist (gated)"
    assert data.auth_check is not None, "data factory needs an auth_check"


def test_no_connect_tool_name_remains() -> None:
    """The connect tool name was removed when connecting moved to the web app."""
    assert not hasattr(ToolName, "SERVICETITAN_CONNECT")


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
    # The reason must steer the agent to the web app, not a chat paste flow.
    assert "web app" in reason.lower()


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
# connect_credentials: end-to-end against the fake (backs the web endpoint)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_connect_credentials_persists_credentials(async_test_user: Any) -> None:
    user_id = async_test_user.id

    cred = await connect_credentials(
        user_id,
        tenant_id="1234567",
        client_id="my-client-id",
        client_secret="my-secret",
    )
    assert cred.tenant_id == "1234567"

    assert await is_connected(user_id) is True
    loaded = await load_credentials(user_id)
    assert loaded is not None
    assert loaded.tenant_id == "1234567"
    assert loaded.client_id == "my-client-id"
    assert loaded.client_secret == "my-secret"
    assert loaded.app_key == "fake-st-app-key"
    assert loaded.access_token == fake_module.FAKE_TOKEN_VALUE
    assert loaded.expires_at > time.time()


@pytest.mark.asyncio()
async def test_connect_credentials_rejects_empty_fields(async_test_user: Any) -> None:
    with pytest.raises(ServiceTitanAuthError, match="required"):
        await connect_credentials(
            async_test_user.id,
            tenant_id="",
            client_id="cid",
            client_secret="csec",
        )


@pytest.mark.asyncio()
async def test_connect_credentials_errors_without_app_key(async_test_user: Any) -> None:
    """When the operator has not set the App Key, connect must fail loudly."""
    from backend.app.config import settings as _settings

    with (
        patch.object(_settings, "servicetitan_app_key", ""),
        pytest.raises(ServiceTitanAuthError, match="App Key"),
    ):
        await connect_credentials(
            async_test_user.id,
            tenant_id="t",
            client_id="cid",
            client_secret="csec",
        )


@pytest.mark.asyncio()
async def test_connect_credentials_strips_and_rejects_blank_client_secret(
    async_test_user: Any,
) -> None:
    """A whitespace-only Client Secret must surface as a validation error."""
    with pytest.raises(ServiceTitanAuthError, match="required"):
        await connect_credentials(
            async_test_user.id,
            tenant_id="t",
            client_id="cid",
            client_secret="   ",  # stripped to empty before the mint call
        )


@pytest.mark.asyncio()
async def test_connect_credentials_surfaces_token_endpoint_failure(
    async_test_user: Any,
) -> None:
    """When the mint endpoint rejects the credentials, the error propagates.

    Patches the mint helper to raise so this test exercises the failure
    path, not the local validation branch that catches blank fields.
    """

    async def _fail(**kwargs: Any) -> tuple[str, float]:
        raise ServiceTitanAuthError("ServiceTitan rejected the client credentials (HTTP 401)")

    with (
        patch(
            "backend.app.integrations.servicetitan.auth.mint_access_token",
            side_effect=_fail,
        ),
        pytest.raises(ServiceTitanAuthError, match="rejected"),
    ):
        await connect_credentials(
            async_test_user.id,
            tenant_id="1234567",
            client_id="cid",
            client_secret="csec",
        )
