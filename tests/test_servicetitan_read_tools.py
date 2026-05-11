"""Tests for the ServiceTitan read tools.

Each tool is exercised end-to-end against a real ``ServiceTitanService``
instance backed by the in-process fake. No live network is touched.
Seed customers and appointments live in
``backend/app/integrations/servicetitan/_fake.py``; the fake mirrors
real-API wire shapes (pagination envelope, ISO date filters, sort
ordering) so the tools' behavior under the fake transfers to live.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import patch

import pytest

from backend.app.agent.tools.names import ToolName
from backend.app.agent.tools.servicetitan_tools import (
    build_servicetitan_tools,
)
from backend.app.integrations.servicetitan import _fake as fake_module
from backend.app.integrations.servicetitan.auth import save_credentials
from backend.app.integrations.servicetitan.service import (
    ServiceTitanService,
    build_service_for_user,
)


@pytest.fixture(autouse=True)
def _force_fake_backend() -> Any:
    """Route every test in this module through the in-process fake backend.

    Resets the process-wide fake between tests so a previous note POST
    or rate-limit override cannot leak. The credential row itself is
    cleaned by the standard async test isolation fixture.
    """
    from backend.app.config import settings as _settings

    with patch.object(_settings, "servicetitan_use_fake", True):
        fake_module.reset_default_fake_backend()
        try:
            yield
        finally:
            fake_module.reset_default_fake_backend()


def _connected_credential_kwargs() -> dict[str, Any]:
    """Credential row matching what a fully connected user holds."""
    return {
        "tenant_id": str(fake_module.DEFAULT_TENANT_ID),
        "client_id": "cid",
        "client_secret": "csec",
        "app_key": "fake-st-app-key",
        "access_token": fake_module.FAKE_TOKEN_VALUE,
        "expires_at": time.time() + 600,
    }


async def _build_connected_service(user_id: str) -> ServiceTitanService:
    """Persist a credential and return a service bound to it."""
    await save_credentials(user_id, **_connected_credential_kwargs())
    service = await build_service_for_user(user_id)
    assert service is not None
    return service


# ---------------------------------------------------------------------------
# Tool wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_build_servicetitan_tools_returns_all_tools(async_test_user: Any) -> None:
    """build_servicetitan_tools must expose the read tools plus st_add_job_note."""
    service = await _build_connected_service(async_test_user.id)
    tools = build_servicetitan_tools(service)
    names = {t.name for t in tools}
    assert names == {
        ToolName.SERVICETITAN_SEARCH_CUSTOMERS,
        ToolName.SERVICETITAN_GET_CUSTOMER,
        ToolName.SERVICETITAN_LIST_APPOINTMENTS,
        ToolName.SERVICETITAN_ADD_JOB_NOTE,
    }


@pytest.mark.asyncio()
async def test_read_tools_have_no_approval_policy_or_concurrency_group(
    async_test_user: Any,
) -> None:
    """Read-only tools must not declare approval policies or concurrency groups."""
    read_only = {
        ToolName.SERVICETITAN_SEARCH_CUSTOMERS,
        ToolName.SERVICETITAN_GET_CUSTOMER,
        ToolName.SERVICETITAN_LIST_APPOINTMENTS,
    }
    service = await _build_connected_service(async_test_user.id)
    for tool in build_servicetitan_tools(service):
        if tool.name not in read_only:
            continue
        assert tool.approval_policy is None, f"{tool.name} should be unrestricted"
        assert tool.concurrency_group is None, f"{tool.name} should not serialize"


def _tool_by_name(tools: list[Any], name: str) -> Any:
    for t in tools:
        if t.name == name:
            return t
    raise AssertionError(f"tool {name} not found")


# ---------------------------------------------------------------------------
# st_search_customers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_search_customers_by_name_returns_match(async_test_user: Any) -> None:
    service = await _build_connected_service(async_test_user.id)
    search = _tool_by_name(
        build_servicetitan_tools(service), ToolName.SERVICETITAN_SEARCH_CUSTOMERS
    )

    result = await search.function(query="Acme")
    assert result.is_error is False
    # Seed "Acme Plumbing" has id 1003 and a phone in the 555 range.
    assert "Acme Plumbing" in result.content
    assert "#1003" in result.content


@pytest.mark.asyncio()
async def test_search_customers_numeric_query_routes_to_phone(
    async_test_user: Any,
) -> None:
    """A digit-heavy query should match against the phone filter."""
    service = await _build_connected_service(async_test_user.id)
    search = _tool_by_name(
        build_servicetitan_tools(service), ToolName.SERVICETITAN_SEARCH_CUSTOMERS
    )

    # Jane Doe (id 1001) has phone +15555550101; "5550101" is enough
    # digits to trip the phone heuristic and uniquely match her record.
    result = await search.function(query="5550101")
    assert result.is_error is False
    assert "Jane Doe" in result.content
    assert "#1001" in result.content


@pytest.mark.asyncio()
async def test_search_customers_empty_query_validation_error(
    async_test_user: Any,
) -> None:
    service = await _build_connected_service(async_test_user.id)
    search = _tool_by_name(
        build_servicetitan_tools(service), ToolName.SERVICETITAN_SEARCH_CUSTOMERS
    )

    result = await search.function(query="   ")
    assert result.is_error is True
    assert "empty" in result.content.lower()


@pytest.mark.asyncio()
async def test_search_customers_no_match_returns_friendly_message(
    async_test_user: Any,
) -> None:
    service = await _build_connected_service(async_test_user.id)
    search = _tool_by_name(
        build_servicetitan_tools(service), ToolName.SERVICETITAN_SEARCH_CUSTOMERS
    )

    result = await search.function(query="ZZZZ-no-such-customer-ZZZZ")
    assert result.is_error is False
    assert "No customers matched" in result.content


@pytest.mark.asyncio()
async def test_search_customers_truncates_to_limit(async_test_user: Any) -> None:
    """A broad name query should respect the requested limit and surface a hint."""
    service = await _build_connected_service(async_test_user.id)
    search = _tool_by_name(
        build_servicetitan_tools(service), ToolName.SERVICETITAN_SEARCH_CUSTOMERS
    )

    # Several seed customers (Jane Doe, John Roe, David Brown, Eve Davis,
    # Bob Johnson, Alice Smith, Carol Williams) include lower-case "e"
    # somewhere in their names. The fake matches case-insensitive
    # substring on the ``name`` filter.
    result = await search.function(query="e", limit=2)
    assert result.is_error is False
    # Header line + truncation note + 2 records.
    lines = result.content.splitlines()
    assert any("matched" in ln for ln in lines)
    record_lines = [ln for ln in lines if ln.startswith("#")]
    assert len(record_lines) == 2
    assert any("more" in ln for ln in lines)


# ---------------------------------------------------------------------------
# st_get_customer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_get_customer_returns_full_record(async_test_user: Any) -> None:
    service = await _build_connected_service(async_test_user.id)
    get_customer = _tool_by_name(
        build_servicetitan_tools(service), ToolName.SERVICETITAN_GET_CUSTOMER
    )

    result = await get_customer.function(customer_id=1003)
    assert result.is_error is False
    assert "Acme Plumbing" in result.content
    assert "Commercial" in result.content
    assert "789 Industry Park" in result.content
    assert "+15555550103" in result.content


@pytest.mark.asyncio()
async def test_get_customer_unknown_id_returns_not_found(async_test_user: Any) -> None:
    service = await _build_connected_service(async_test_user.id)
    get_customer = _tool_by_name(
        build_servicetitan_tools(service), ToolName.SERVICETITAN_GET_CUSTOMER
    )

    result = await get_customer.function(customer_id=999999)
    assert result.is_error is True
    assert result.error_kind is not None
    assert result.error_kind.value == "not_found"
    assert "999999" in result.content


# ---------------------------------------------------------------------------
# st_list_appointments
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_list_appointments_with_wide_range_returns_all(
    async_test_user: Any,
) -> None:
    """A range that covers every seed appointment should return all 15."""
    service = await _build_connected_service(async_test_user.id)
    list_appts = _tool_by_name(
        build_servicetitan_tools(service), ToolName.SERVICETITAN_LIST_APPOINTMENTS
    )

    # Seed appointments cluster around 2026-05-11. Use a wide window.
    result = await list_appts.function(
        from_date="2026-01-01T00:00:00Z",
        to_date="2027-01-01T00:00:00Z",
    )
    assert result.is_error is False
    assert "Found 15 appointment(s)" in result.content


@pytest.mark.asyncio()
async def test_list_appointments_status_filter_narrows(async_test_user: Any) -> None:
    """The status filter should narrow the result set by exact status."""
    service = await _build_connected_service(async_test_user.id)
    list_appts = _tool_by_name(
        build_servicetitan_tools(service), ToolName.SERVICETITAN_LIST_APPOINTMENTS
    )

    wide_window = {
        "from_date": "2026-01-01T00:00:00Z",
        "to_date": "2027-01-01T00:00:00Z",
    }
    full = await list_appts.function(**wide_window)
    scheduled = await list_appts.function(status="Scheduled", **wide_window)
    assert full.is_error is False
    assert scheduled.is_error is False
    # The seed mixes statuses across the 15 appointments; the scheduled
    # subset must be strictly smaller than the full set.
    full_lines = [ln for ln in full.content.splitlines() if ln.startswith("#")]
    scheduled_lines = [ln for ln in scheduled.content.splitlines() if ln.startswith("#")]
    assert 0 < len(scheduled_lines) < len(full_lines)
    for ln in scheduled_lines:
        assert "[Scheduled]" in ln


@pytest.mark.asyncio()
async def test_list_appointments_empty_window_reports_no_match(
    async_test_user: Any,
) -> None:
    service = await _build_connected_service(async_test_user.id)
    list_appts = _tool_by_name(
        build_servicetitan_tools(service), ToolName.SERVICETITAN_LIST_APPOINTMENTS
    )

    result = await list_appts.function(
        from_date="2099-01-01T00:00:00Z",
        to_date="2099-01-02T00:00:00Z",
    )
    assert result.is_error is False
    assert "No ServiceTitan appointments found" in result.content


@pytest.mark.asyncio()
async def test_list_appointments_defaults_to_today_when_no_dates(
    async_test_user: Any,
) -> None:
    """No-argument call should request the day's window from the API.

    The seed today is 2026-05-11; the test today (per
    ``datetime.now(UTC)`` at run time) may differ, so we cannot assert
    on a specific record count. Instead, we assert the tool does not
    error and that the message is the expected "Found ..." or "No ...
    appointments found" form, which proves the default window was
    computed and the request issued.
    """
    service = await _build_connected_service(async_test_user.id)
    list_appts = _tool_by_name(
        build_servicetitan_tools(service), ToolName.SERVICETITAN_LIST_APPOINTMENTS
    )

    result = await list_appts.function()
    assert result.is_error is False
    first_line = result.content.splitlines()[0]
    assert first_line.startswith("Found ") or first_line.startswith(
        "No ServiceTitan appointments found"
    )
