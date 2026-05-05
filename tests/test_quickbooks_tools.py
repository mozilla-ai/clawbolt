"""Tests for QuickBooks Online tools."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from backend.app.agent.tools.base import Tool
from backend.app.agent.tools.registry import ToolContext
from backend.app.integrations.quickbooks.factory import (
    _describe_qb_query,
    _format_intuit_fault,
    _format_results,
    _quickbooks_factory,
    create_quickbooks_tools,
)
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
    """Should return estimates with SyncToken visible."""
    result = await qb_tool.function(query="SELECT * FROM Estimate")

    assert result.is_error is False
    assert "1 result(s)" in result.content
    assert "EST-2001" in result.content
    assert "SyncToken: 0" in result.content


@pytest.mark.asyncio()
async def test_query_items(qb_tool: Tool) -> None:
    """Should return items."""
    result = await qb_tool.function(query="SELECT * FROM Item")

    assert result.is_error is False
    assert "Drywall" in result.content


@pytest.mark.asyncio()
async def test_query_invoices_includes_line_items(qb_tool: Tool) -> None:
    """Query results should include line item details, not just a count."""
    result = await qb_tool.function(query="SELECT * FROM Invoice")

    assert result.is_error is False
    assert "Pipe repair labor $350.00" in result.content
    assert "Copper fittings $150.00" in result.content
    assert "Kitchen remodel labor $800.00" in result.content


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
    """All QB tools must have a params_model set."""
    tools = create_quickbooks_tools(qb_service)
    for tool in tools:
        assert tool.params_model is not None, f"Tool {tool.name} missing params_model"


def test_quickbooks_tools_count(qb_service: MockQuickBooksService) -> None:
    """create_quickbooks_tools should return 4 tools."""
    tools = create_quickbooks_tools(qb_service)
    assert len(tools) == 4


@pytest.mark.asyncio()
async def test_quickbooks_factory_returns_empty_when_not_configured() -> None:
    """_quickbooks_factory should return [] when client_id/secret are empty."""
    ctx = MagicMock(spec=ToolContext)
    user = MagicMock(spec=User)
    user.id = 1
    ctx.user = user

    with (
        patch("backend.app.integrations.quickbooks.factory.settings") as mock_settings,
    ):
        mock_settings.quickbooks_client_id = ""
        mock_settings.quickbooks_client_secret = ""
        assert await _quickbooks_factory(ctx) == []


@pytest.mark.asyncio()
async def test_quickbooks_factory_returns_empty_when_not_connected() -> None:
    """_quickbooks_factory should return [] when user has no OAuth token."""
    ctx = MagicMock(spec=ToolContext)
    user = MagicMock(spec=User)
    user.id = 1
    ctx.user = user

    with (
        patch("backend.app.integrations.quickbooks.factory.settings") as mock_settings,
        patch("backend.app.integrations.quickbooks.factory.oauth_service") as mock_oauth,
    ):
        mock_settings.quickbooks_client_id = "test-id"
        mock_settings.quickbooks_client_secret = "test-secret"
        mock_oauth.get_valid_token = AsyncMock(return_value=None)

        tools = await _quickbooks_factory(ctx)

    assert tools == []


# -- _describe_qb_query --


class TestDescribeQbQuery:
    def test_known_entity(self) -> None:
        """Known entity returns human-readable label."""
        desc = _describe_qb_query({"query": "SELECT * FROM Estimate ORDERBY TxnDate DESC"})
        assert desc == "Look up estimates in QuickBooks"

    def test_unknown_entity(self) -> None:
        """Unknown entity falls back to lowercased name + 's'."""
        desc = _describe_qb_query({"query": "SELECT * FROM Widget"})
        assert desc == "Look up widgets in QuickBooks"

    def test_no_from_clause(self) -> None:
        """Missing FROM clause returns generic fallback."""
        desc = _describe_qb_query({"query": "SELECT something"})
        assert desc == "Look up data in QuickBooks"

    def test_salesreceipt_label(self) -> None:
        """Multi-word entity labels are correct."""
        desc = _describe_qb_query({"query": "SELECT * FROM SalesReceipt"})
        assert desc == "Look up sales receipts in QuickBooks"

    def test_empty_query(self) -> None:
        """Empty query returns generic fallback."""
        desc = _describe_qb_query({})
        assert desc == "Look up data in QuickBooks"


# -- Intuit fault formatting --


def _make_http_error(status: int, body: object) -> httpx.HTTPStatusError:
    """Build a fake HTTPStatusError with a JSON body for the formatter."""
    request = httpx.Request("GET", "https://example.invalid/q")
    response = httpx.Response(status, json=body, request=request)
    return httpx.HTTPStatusError("error", request=request, response=response)


class TestFormatIntuitFault:
    """The error formatter is the one place that converts QBO's nested
    ``Fault.Error[]`` JSON into a single line the LLM can act on. These
    tests pin the contract: the formatter must always return SOMETHING
    useful, even on malformed input."""

    def test_pulls_message_and_detail_from_fault(self) -> None:
        body = {
            "Fault": {
                "Error": [
                    {
                        "Message": "Invalid enumeration value",
                        "Detail": "TxnStatus 'In Progress' is not valid.",
                        "code": "2030",
                    }
                ],
                "type": "ValidationFault",
            }
        }
        out = _format_intuit_fault(_make_http_error(400, body), entity="Estimate")
        assert "code=2030" in out
        assert "Invalid enumeration value" in out
        assert "TxnStatus 'In Progress' is not valid" in out

    def test_appends_enum_hint_when_entity_is_known(self) -> None:
        """A 400 mentioning TxnStatus on Estimate should append the
        full valid set so the model self-corrects on the next turn."""
        body = {
            "Fault": {
                "Error": [{"Message": "validation", "Detail": "TxnStatus 'Frozen' is invalid"}]
            }
        }
        out = _format_intuit_fault(_make_http_error(400, body), entity="Estimate")
        assert "Hint: Valid TxnStatus for Estimate:" in out
        for valid in ("Pending", "Accepted", "Closed", "Rejected"):
            assert valid in out

    def test_no_hint_when_entity_unknown(self) -> None:
        """The hint set is curated; unknown entities get the raw error only."""
        body = {"Fault": {"Error": [{"Message": "X", "Detail": "TxnStatus blah"}]}}
        out = _format_intuit_fault(_make_http_error(400, body), entity="Customer")
        assert "Hint:" not in out

    def test_falls_back_to_raw_body_on_unexpected_shape(self) -> None:
        """When QBO returns something unfaultlike (e.g. an HTML 502), the
        formatter must still return a non-empty message containing the
        status code so the LLM is not left guessing."""
        request = httpx.Request("GET", "https://example.invalid/q")
        response = httpx.Response(502, content=b"<html>Bad Gateway</html>", request=request)
        exc = httpx.HTTPStatusError("error", request=request, response=response)
        out = _format_intuit_fault(exc, entity="Estimate")
        assert "502" in out
        assert "Bad Gateway" in out


# -- Query result formatter --


class TestFormatResults:
    """The query result formatter is the agent's only window into QBO records.
    Each known dict-envelope shape (Address, FreeFormNumber, URI, postal
    address, name+value ref) must surface; unknown shapes must fail loud
    via json.dumps rather than silently disappearing."""

    def test_zero_rows(self) -> None:
        assert _format_results([]) == "Query returned 0 results."

    def test_name_value_ref(self) -> None:
        """{name, value} refs format as 'name (value)'."""
        out = _format_results([{"Id": "1", "CustomerRef": {"name": "Acme", "value": "42"}}])
        assert "CustomerRef: Acme (42)" in out

    def test_value_only_ref(self) -> None:
        """A ref with only `value` (e.g. CustomerMemo) keeps the field name."""
        out = _format_results([{"Id": "1", "CustomerMemo": {"value": "Thanks!"}}])
        assert "CustomerMemo: Thanks!" in out

    def test_name_only_ref(self) -> None:
        """A ref with only `name` keeps the field name and shows the name."""
        out = _format_results([{"Id": "1", "CustomerRef": {"name": "Acme"}}])
        assert "CustomerRef: Acme" in out

    def test_email_address_envelope(self) -> None:
        """{Address} envelopes (BillEmail, PrimaryEmailAddr) surface the email."""
        out = _format_results(
            [
                {
                    "Id": "1",
                    "BillEmail": {"Address": "ar@example.com"},
                    "PrimaryEmailAddr": {"Address": "primary@example.com"},
                }
            ]
        )
        assert "BillEmail: ar@example.com" in out
        assert "PrimaryEmailAddr: primary@example.com" in out

    def test_phone_freeformnumber_envelope(self) -> None:
        """{FreeFormNumber} envelopes (PrimaryPhone, Mobile, etc.) surface the number."""
        out = _format_results(
            [
                {
                    "Id": "1",
                    "PrimaryPhone": {"FreeFormNumber": "555-0100"},
                    "Mobile": {"FreeFormNumber": "555-0200"},
                }
            ]
        )
        assert "PrimaryPhone: 555-0100" in out
        assert "Mobile: 555-0200" in out

    def test_web_uri_envelope(self) -> None:
        """{URI} envelopes (WebAddr) surface the URL."""
        out = _format_results([{"Id": "1", "WebAddr": {"URI": "https://example.com"}}])
        assert "WebAddr: https://example.com" in out

    def test_postal_address_envelope(self) -> None:
        """Address-shaped dicts (BillAddr, ShipAddr) join their parts."""
        out = _format_results(
            [
                {
                    "Id": "1",
                    "BillAddr": {
                        "Line1": "123 Main St",
                        "City": "Pittsburgh",
                        "CountrySubDivisionCode": "PA",
                        "PostalCode": "15220",
                    },
                }
            ]
        )
        assert "BillAddr: 123 Main St, Pittsburgh, PA, 15220" in out

    def test_unknown_dict_shape_is_json_dumped(self) -> None:
        """Unknown dict shapes must surface via json.dumps, never silently drop."""
        out = _format_results([{"Id": "1", "MysteryField": {"weird": "shape"}}])
        assert 'MysteryField: {"weird": "shape"}' in out

    def test_customer_with_email_and_address_regression(self) -> None:
        """Regression for issue #1137: a Customer query result with both
        PrimaryEmailAddr and BillAddr must surface both fields. Before the
        fix, both were silently dropped because their dicts had neither
        `name` nor `value`."""
        row = {
            "Id": "540",
            "DisplayName": "Acme Plumbing",
            "BillEmail": {"Address": "billing@example.com"},
            "PrimaryEmailAddr": {"Address": "primary@example.com"},
            "PrimaryPhone": {"FreeFormNumber": "555-1234"},
            "BillAddr": {"Line1": "1 Test Ave", "City": "Pittsburgh"},
            "CustomerRef": {"name": "Acme Plumbing", "value": "16"},
        }
        out = _format_results([row])
        assert "BillEmail: billing@example.com" in out
        assert "PrimaryEmailAddr: primary@example.com" in out
        assert "PrimaryPhone: 555-1234" in out
        assert "BillAddr: 1 Test Ave, Pittsburgh" in out
        assert "CustomerRef: Acme Plumbing (16)" in out
