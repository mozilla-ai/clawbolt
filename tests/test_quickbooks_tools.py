"""Tests for QuickBooks Online tools."""

from __future__ import annotations

import pytest

from backend.app.agent.tools.base import Tool
from backend.app.agent.tools.quickbooks_tools import create_quickbooks_tools
from tests.mocks.quickbooks import MockQuickBooksService


@pytest.fixture()
def qb_service() -> MockQuickBooksService:
    return MockQuickBooksService()


@pytest.fixture()
def qb_tools(qb_service: MockQuickBooksService) -> dict[str, Tool]:
    """Create QuickBooks tools and return them keyed by name."""
    tools = create_quickbooks_tools(qb_service)
    return {t.name: t for t in tools}


# -- Search invoices --


@pytest.mark.asyncio()
async def test_search_invoices_returns_all(qb_tools: dict[str, Tool]) -> None:
    """qb_search_invoices with no filter should return all invoices."""
    tool = qb_tools["qb_search_invoices"]
    result = await tool.function()

    assert result.is_error is False
    assert "2 invoice(s)" in result.content
    assert "INV-1001" in result.content
    assert "INV-1002" in result.content


@pytest.mark.asyncio()
async def test_search_invoices_by_customer(qb_tools: dict[str, Tool]) -> None:
    """qb_search_invoices should filter by customer name."""
    tool = qb_tools["qb_search_invoices"]
    result = await tool.function(customer_name="Jane")

    assert result.is_error is False
    assert "1 invoice(s)" in result.content
    assert "Jane Doe" in result.content
    assert "John Smith" not in result.content


@pytest.mark.asyncio()
async def test_search_invoices_no_match(qb_tools: dict[str, Tool]) -> None:
    """qb_search_invoices should report when no invoices match."""
    tool = qb_tools["qb_search_invoices"]
    result = await tool.function(customer_name="Nobody")

    assert result.is_error is False
    assert "No invoices found" in result.content


@pytest.mark.asyncio()
async def test_search_invoices_shows_payment_status(qb_tools: dict[str, Tool]) -> None:
    """qb_search_invoices should indicate paid vs open status."""
    tool = qb_tools["qb_search_invoices"]
    result = await tool.function()

    assert "Paid" in result.content
    assert "Open" in result.content


@pytest.mark.asyncio()
async def test_search_invoices_api_error(qb_service: MockQuickBooksService) -> None:
    """qb_search_invoices should handle API errors gracefully."""

    async def failing(customer_name: str | None = None) -> list[dict]:
        raise RuntimeError("API connection failed")

    qb_service.list_invoices = failing  # type: ignore[assignment]
    tools = create_quickbooks_tools(qb_service)
    tool = {t.name: t for t in tools}["qb_search_invoices"]
    result = await tool.function()

    assert result.is_error is True
    assert "Error" in result.content


# -- Search estimates --


@pytest.mark.asyncio()
async def test_search_estimates_returns_all(qb_tools: dict[str, Tool]) -> None:
    """qb_search_estimates with no filter should return all estimates."""
    tool = qb_tools["qb_search_estimates"]
    result = await tool.function()

    assert result.is_error is False
    assert "2 estimate(s)" in result.content
    assert "EST-2001" in result.content
    assert "EST-2002" in result.content


@pytest.mark.asyncio()
async def test_search_estimates_by_customer(qb_tools: dict[str, Tool]) -> None:
    """qb_search_estimates should filter by customer name."""
    tool = qb_tools["qb_search_estimates"]
    result = await tool.function(customer_name="John")

    assert result.is_error is False
    assert "1 estimate(s)" in result.content
    assert "John Smith" in result.content
    assert "Jane Doe" not in result.content


@pytest.mark.asyncio()
async def test_search_estimates_no_match(qb_tools: dict[str, Tool]) -> None:
    """qb_search_estimates should report when no estimates match."""
    tool = qb_tools["qb_search_estimates"]
    result = await tool.function(customer_name="Nobody")

    assert result.is_error is False
    assert "No estimates found" in result.content


@pytest.mark.asyncio()
async def test_search_estimates_shows_status(qb_tools: dict[str, Tool]) -> None:
    """qb_search_estimates should show estimate status."""
    tool = qb_tools["qb_search_estimates"]
    result = await tool.function()

    assert "Accepted" in result.content
    assert "Pending" in result.content


@pytest.mark.asyncio()
async def test_search_estimates_api_error(qb_service: MockQuickBooksService) -> None:
    """qb_search_estimates should handle API errors gracefully."""

    async def failing(customer_name: str | None = None) -> list[dict]:
        raise RuntimeError("API connection failed")

    qb_service.list_estimates = failing  # type: ignore[assignment]
    tools = create_quickbooks_tools(qb_service)
    tool = {t.name: t for t in tools}["qb_search_estimates"]
    result = await tool.function()

    assert result.is_error is True
    assert "Error" in result.content


# -- Tool registration --


def test_quickbooks_tools_have_params_models(qb_service: MockQuickBooksService) -> None:
    """All QuickBooks tools must have a params_model set."""
    tools = create_quickbooks_tools(qb_service)
    for tool in tools:
        assert tool.params_model is not None, f"Tool {tool.name} missing params_model"


def test_quickbooks_tools_count(qb_service: MockQuickBooksService) -> None:
    """create_quickbooks_tools should return 2 tools."""
    tools = create_quickbooks_tools(qb_service)
    assert len(tools) == 2


def test_quickbooks_factory_returns_empty_when_not_configured() -> None:
    """_quickbooks_factory should return [] when QuickBooks is not configured."""
    from unittest.mock import MagicMock, patch

    from backend.app.agent.tools.quickbooks_tools import _quickbooks_factory
    from backend.app.agent.tools.registry import ToolContext

    ctx = MagicMock(spec=ToolContext)

    with patch(
        "backend.app.agent.tools.quickbooks_tools.get_quickbooks_service",
        return_value=None,
    ):
        tools = _quickbooks_factory(ctx)
    assert tools == []
