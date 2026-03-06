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


# -- Search items --


@pytest.mark.asyncio()
async def test_search_items_returns_results(qb_tools: dict[str, Tool]) -> None:
    """qb_search_items should return matching items."""
    tool = qb_tools["qb_search_items"]
    result = await tool.function(query="Drywall")

    assert result.is_error is False
    assert "Drywall Sheet 4x8" in result.content
    assert "$12.50" in result.content


@pytest.mark.asyncio()
async def test_search_items_no_match(qb_tools: dict[str, Tool]) -> None:
    """qb_search_items should report when no items match."""
    tool = qb_tools["qb_search_items"]
    result = await tool.function(query="Nonexistent Widget")

    assert result.is_error is False
    assert "No items found" in result.content


@pytest.mark.asyncio()
async def test_search_items_api_error(qb_service: MockQuickBooksService) -> None:
    """qb_search_items should handle API errors gracefully."""

    async def failing_list_items(query: str | None = None) -> list[dict]:
        raise RuntimeError("API connection failed")

    qb_service.list_items = failing_list_items  # type: ignore[assignment]
    tools = create_quickbooks_tools(qb_service)
    tool = tools[0]
    result = await tool.function(query="anything")

    assert result.is_error is True
    assert "Error" in result.content


# -- Search customers --


@pytest.mark.asyncio()
async def test_search_customers_returns_results(qb_tools: dict[str, Tool]) -> None:
    """qb_search_customers should return matching customers."""
    tool = qb_tools["qb_search_customers"]
    result = await tool.function(query="John")

    assert result.is_error is False
    assert "John Smith" in result.content
    assert "john@example.com" in result.content


@pytest.mark.asyncio()
async def test_search_customers_no_match(qb_tools: dict[str, Tool]) -> None:
    """qb_search_customers should report when no customers match."""
    tool = qb_tools["qb_search_customers"]
    result = await tool.function(query="Nobody Here")

    assert result.is_error is False
    assert "No customers found" in result.content


@pytest.mark.asyncio()
async def test_search_customers_api_error(qb_service: MockQuickBooksService) -> None:
    """qb_search_customers should handle API errors gracefully."""

    async def failing_list_customers(query: str | None = None) -> list[dict]:
        raise RuntimeError("API connection failed")

    qb_service.list_customers = failing_list_customers  # type: ignore[assignment]
    tools = create_quickbooks_tools(qb_service)
    tool = tools[1]
    result = await tool.function(query="anyone")

    assert result.is_error is True
    assert "Error" in result.content


# -- Create invoice --


@pytest.mark.asyncio()
async def test_create_invoice_success(
    qb_tools: dict[str, Tool],
    qb_service: MockQuickBooksService,
) -> None:
    """qb_create_invoice should create an invoice and return details."""
    tool = qb_tools["qb_create_invoice"]
    result = await tool.function(
        customer_id="100",
        line_items=[
            {"description": "Drywall install", "quantity": 10, "unit_price": 12.50},
        ],
    )

    assert result.is_error is False
    assert "Invoice created" in result.content
    assert "$125.00" in result.content
    assert len(qb_service.invoices) == 1


@pytest.mark.asyncio()
async def test_create_invoice_empty_line_items(qb_tools: dict[str, Tool]) -> None:
    """qb_create_invoice should reject empty line items."""
    tool = qb_tools["qb_create_invoice"]
    result = await tool.function(customer_id="100", line_items=[])

    assert result.is_error is True
    assert "at least one line item" in result.content


@pytest.mark.asyncio()
async def test_create_invoice_api_error(qb_service: MockQuickBooksService) -> None:
    """qb_create_invoice should handle API errors gracefully."""

    async def failing_create_invoice(customer_id: str, line_items: list[dict]) -> dict:
        raise RuntimeError("QBO API error")

    qb_service.create_invoice = failing_create_invoice  # type: ignore[assignment]
    tools = create_quickbooks_tools(qb_service)
    tool = tools[2]
    result = await tool.function(
        customer_id="100",
        line_items=[{"description": "Work", "quantity": 1, "unit_price": 100}],
    )

    assert result.is_error is True
    assert "Error" in result.content


# -- Send invoice --


@pytest.mark.asyncio()
async def test_send_invoice_success(
    qb_service: MockQuickBooksService,
) -> None:
    """qb_send_invoice should send an invoice via email."""
    # First create an invoice
    invoice = await qb_service.create_invoice(
        "100", [{"amount": 100, "description": "Work", "quantity": 1, "unit_price": 100}]
    )
    tools = create_quickbooks_tools(qb_service)
    tool = tools[3]
    result = await tool.function(invoice_id=invoice["id"])

    assert result.is_error is False
    assert "sent successfully" in result.content
    assert invoice["id"] in qb_service.sent_invoices


@pytest.mark.asyncio()
async def test_send_invoice_not_found(qb_service: MockQuickBooksService) -> None:
    """qb_send_invoice should handle missing invoice."""
    tools = create_quickbooks_tools(qb_service)
    tool = tools[3]
    result = await tool.function(invoice_id="99999")

    assert result.is_error is True
    assert "Error" in result.content


@pytest.mark.asyncio()
async def test_send_invoice_api_error(qb_service: MockQuickBooksService) -> None:
    """qb_send_invoice should handle API errors gracefully."""

    async def failing_send(invoice_id: str) -> dict:
        raise RuntimeError("Email service unavailable")

    qb_service.send_invoice = failing_send  # type: ignore[assignment]
    tools = create_quickbooks_tools(qb_service)
    tool = tools[3]
    result = await tool.function(invoice_id="1000")

    assert result.is_error is True
    assert "Error" in result.content


# -- Tool registration --


def test_quickbooks_tools_have_params_models(qb_service: MockQuickBooksService) -> None:
    """All QuickBooks tools must have a params_model set."""
    tools = create_quickbooks_tools(qb_service)
    for tool in tools:
        assert tool.params_model is not None, f"Tool {tool.name} missing params_model"


def test_quickbooks_tools_count(qb_service: MockQuickBooksService) -> None:
    """create_quickbooks_tools should return 4 tools."""
    tools = create_quickbooks_tools(qb_service)
    assert len(tools) == 4


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
