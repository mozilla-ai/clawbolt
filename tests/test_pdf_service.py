import pytest

from backend.app.services.pdf_service import EstimatePDFData, generate_estimate_pdf


def _make_estimate_data(**kwargs: object) -> EstimatePDFData:
    """Create test estimate data with defaults."""
    defaults: dict[str, object] = {
        "owner_name": "Mike Chen",
        "owner_phone": "(555) 123-4567",
        "owner_trade": "General User",
        "description": "Deck construction and repair",
        "line_items": [
            {"description": "Deck framing", "quantity": 1, "unit_price": 2400, "total": 2400},
            {"description": "Composite decking", "quantity": 1, "unit_price": 3200, "total": 3200},
        ],
        "subtotal": 5600.0,
        "total": 5600.0,
        "estimate_date": "Feb 28, 2026",
        "estimate_number": "EST-001",
        "client_name": "John Smith",
        "client_address": "123 Oak St, Portland OR",
        "terms": "50% deposit, balance on completion. Estimate valid 30 days.",
    }
    defaults.update(kwargs)
    return EstimatePDFData(**defaults)  # type: ignore[arg-type]


@pytest.mark.asyncio()
async def test_generate_pdf_valid_bytes() -> None:
    """Generated PDF should start with PDF magic bytes."""
    data = _make_estimate_data()
    pdf_bytes = await generate_estimate_pdf(data)
    assert pdf_bytes[:5] == b"%PDF-"
    assert len(pdf_bytes) > 100


@pytest.mark.asyncio()
async def test_generate_pdf_no_client() -> None:
    """PDF should generate successfully without client info."""
    data = _make_estimate_data(client_name=None, client_address=None)
    pdf_bytes = await generate_estimate_pdf(data)
    assert pdf_bytes[:5] == b"%PDF-"


@pytest.mark.asyncio()
async def test_generate_pdf_no_terms() -> None:
    """PDF should generate successfully without terms."""
    data = _make_estimate_data(terms=None)
    pdf_bytes = await generate_estimate_pdf(data)
    assert pdf_bytes[:5] == b"%PDF-"


@pytest.mark.asyncio()
async def test_generate_pdf_with_tax() -> None:
    """PDF should include tax line when provided."""
    data = _make_estimate_data(tax_rate=8.5, tax_amount=476.0, total=6076.0)
    pdf_bytes = await generate_estimate_pdf(data)
    assert pdf_bytes[:5] == b"%PDF-"
    assert len(pdf_bytes) > 100


@pytest.mark.asyncio()
async def test_generate_pdf_single_line_item() -> None:
    """PDF should work with a single line item."""
    data = _make_estimate_data(
        line_items=[
            {"description": "Deck framing", "quantity": 1, "unit_price": 2400, "total": 2400}
        ],
        subtotal=2400.0,
        total=2400.0,
    )
    pdf_bytes = await generate_estimate_pdf(data)
    assert pdf_bytes[:5] == b"%PDF-"
