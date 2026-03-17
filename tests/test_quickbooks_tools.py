"""Tests for QuickBooks Online tools."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from backend.app.agent.tools.base import Tool
from backend.app.agent.tools.quickbooks_tools import (
    _quickbooks_factory,
    create_quickbooks_tools,
)
from backend.app.agent.tools.registry import ToolContext
from backend.app.models import User
from tests.mocks.quickbooks import MockQuickBooksService


@pytest.fixture()
def qb_service() -> MockQuickBooksService:
    return MockQuickBooksService()


@pytest.fixture()
def qb_tool(qb_service: MockQuickBooksService) -> Tool:
    """Create the qb_query tool."""
    tools = create_quickbooks_tools(qb_service)
    return tools[0]


# -- Basic queries --


@pytest.mark.asyncio()
async def test_query_invoices(qb_tool: Tool) -> None:
    """Should return all invoices."""
    result = await qb_tool.function(query="SELECT * FROM Invoice")

    assert result.is_error is False
    assert "2 result(s)" in result.content
    assert "INV-1001" in result.content
    assert "INV-1002" in result.content


@pytest.mark.asyncio()
async def test_query_customers(qb_tool: Tool) -> None:
    """Should return all customers."""
    result = await qb_tool.function(query="SELECT * FROM Customer")

    assert result.is_error is False
    assert "2 result(s)" in result.content
    assert "John Smith" in result.content
    assert "Jane Doe" in result.content


@pytest.mark.asyncio()
async def test_query_estimates(qb_tool: Tool) -> None:
    """Should return estimates."""
    result = await qb_tool.function(query="SELECT * FROM Estimate")

    assert result.is_error is False
    assert "1 result(s)" in result.content
    assert "EST-2001" in result.content


@pytest.mark.asyncio()
async def test_query_items(qb_tool: Tool) -> None:
    """Should return items."""
    result = await qb_tool.function(query="SELECT * FROM Item")

    assert result.is_error is False
    assert "Drywall" in result.content


# -- Filtering --


@pytest.mark.asyncio()
async def test_query_with_like_filter(qb_tool: Tool) -> None:
    """WHERE LIKE should filter results."""
    result = await qb_tool.function(query="SELECT * FROM Customer WHERE DisplayName LIKE '%John%'")

    assert result.is_error is False
    assert "1 result(s)" in result.content
    assert "John Smith" in result.content
    assert "Jane" not in result.content


@pytest.mark.asyncio()
async def test_query_with_maxresults(qb_tool: Tool) -> None:
    """MAXRESULTS should limit rows."""
    result = await qb_tool.function(query="SELECT * FROM Invoice MAXRESULTS 1")

    assert result.is_error is False
    assert "1 result(s)" in result.content


@pytest.mark.asyncio()
async def test_query_no_results(qb_tool: Tool) -> None:
    """Query with no matches should return 0 results message."""
    result = await qb_tool.function(
        query="SELECT * FROM Customer WHERE DisplayName LIKE '%Nobody%'"
    )

    assert result.is_error is False
    assert "0 results" in result.content


# -- Validation --


@pytest.mark.asyncio()
async def test_query_rejects_non_select(qb_tool: Tool) -> None:
    """Non-SELECT queries should be rejected."""
    result = await qb_tool.function(query="DELETE FROM Invoice WHERE Id = '1'")

    assert result.is_error is True
    assert "SELECT" in result.content


# -- Error handling --


@pytest.mark.asyncio()
async def test_query_api_error(qb_service: MockQuickBooksService) -> None:
    """API errors should be returned gracefully."""

    async def failing(query_str: str) -> list[dict]:
        raise RuntimeError("API connection failed")

    qb_service.query = failing  # type: ignore[assignment]
    tools = create_quickbooks_tools(qb_service)
    tool = tools[0]
    result = await tool.function(query="SELECT * FROM Invoice")

    assert result.is_error is True
    assert "error" in result.content.lower()


# -- Tool registration --


def test_quickbooks_tools_have_params_model(qb_service: MockQuickBooksService) -> None:
    """The qb_query tool must have a params_model set."""
    tools = create_quickbooks_tools(qb_service)
    for tool in tools:
        assert tool.params_model is not None, f"Tool {tool.name} missing params_model"


def test_quickbooks_tools_count(qb_service: MockQuickBooksService) -> None:
    """create_quickbooks_tools should return 6 tools."""
    tools = create_quickbooks_tools(qb_service)
    assert len(tools) == 6


def test_quickbooks_factory_returns_empty_when_not_configured() -> None:
    """_quickbooks_factory should return [] when client_id/secret are empty."""
    ctx = MagicMock(spec=ToolContext)
    user = MagicMock(spec=User)
    user.id = 1
    ctx.user = user

    with (
        patch("backend.app.agent.tools.quickbooks_tools.settings") as mock_settings,
    ):
        mock_settings.quickbooks_client_id = ""
        mock_settings.quickbooks_client_secret = ""
        assert _quickbooks_factory(ctx) == []


def test_quickbooks_factory_returns_empty_when_not_connected() -> None:
    """_quickbooks_factory should return [] when user has no OAuth token."""
    ctx = MagicMock(spec=ToolContext)
    user = MagicMock(spec=User)
    user.id = 1
    ctx.user = user

    with (
        patch("backend.app.agent.tools.quickbooks_tools.settings") as mock_settings,
        patch("backend.app.agent.tools.quickbooks_tools.oauth_service") as mock_oauth,
    ):
        mock_settings.quickbooks_client_id = "test-id"
        mock_settings.quickbooks_client_secret = "test-secret"
        mock_oauth.load_token.return_value = None

        tools = _quickbooks_factory(ctx)

    assert tools == []
