from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.app.agent.tools.estimate_tools import _next_estimate_number, create_estimate_tools
from backend.app.models import Contractor, Estimate, EstimateLineItem


@pytest.mark.asyncio()
async def test_generate_estimate_creates_records(
    db_session: Session,
    test_contractor: Contractor,
) -> None:
    """generate_estimate should create Estimate and EstimateLineItem records."""
    tools = create_estimate_tools(db_session, test_contractor)
    generate = tools[0].function

    result = await generate(
        description="12x12 composite deck build",
        line_items=[
            {"description": "Materials - Trex composite", "quantity": 1, "unit_price": 2400.00},
            {"description": "Labor - deck build", "quantity": 24, "unit_price": 75.00},
        ],
    )

    assert "EST-0001" in result
    assert "$4,200.00" in result

    estimate = (
        db_session.query(Estimate).filter(Estimate.contractor_id == test_contractor.id).first()
    )
    assert estimate is not None
    assert estimate.total_amount == 4200.00
    assert estimate.status == "sent"
    assert estimate.description == "12x12 composite deck build"

    items = (
        db_session.query(EstimateLineItem).filter(EstimateLineItem.estimate_id == estimate.id).all()
    )
    assert len(items) == 2


@pytest.mark.asyncio()
async def test_generate_estimate_with_client_info(
    db_session: Session,
    test_contractor: Contractor,
) -> None:
    """generate_estimate should include client info."""
    tools = create_estimate_tools(db_session, test_contractor)
    generate = tools[0].function

    result = await generate(
        description="Bathroom remodel",
        line_items=[{"description": "Full remodel", "quantity": 1, "unit_price": 8500.00}],
        client_name="John Johnson",
        client_address="123 Oak St, Portland, OR",
    )

    assert "EST-0001" in result
    assert "$8,500.00" in result


@pytest.mark.asyncio()
async def test_generate_estimate_pdf_generated(
    db_session: Session,
    test_contractor: Contractor,
) -> None:
    """generate_estimate should generate a PDF (nonzero bytes)."""
    tools = create_estimate_tools(db_session, test_contractor)
    generate = tools[0].function

    result = await generate(
        description="Quick fix",
        line_items=[{"description": "Service call", "quantity": 1, "unit_price": 150.00}],
    )

    # Result mentions PDF URL
    assert "/api/estimates/" in result
    assert "/pdf" in result

    # Verify PDF file was actually written
    from pathlib import Path

    estimate = db_session.query(Estimate).first()
    pdf_path = Path(f"data/estimates/{estimate.id}.pdf")
    assert pdf_path.exists()
    assert pdf_path.stat().st_size > 0
    # Clean up
    pdf_path.unlink()


@pytest.mark.asyncio()
async def test_generate_estimate_sequential_numbers(
    db_session: Session,
    test_contractor: Contractor,
) -> None:
    """Estimate numbers should be sequential per contractor."""
    tools = create_estimate_tools(db_session, test_contractor)
    generate = tools[0].function

    result1 = await generate(
        description="Job 1",
        line_items=[{"description": "Work", "quantity": 1, "unit_price": 100.00}],
    )
    result2 = await generate(
        description="Job 2",
        line_items=[{"description": "Work", "quantity": 1, "unit_price": 200.00}],
    )

    assert "EST-0001" in result1
    assert "EST-0002" in result2


@pytest.mark.asyncio()
async def test_generate_estimate_single_line_item(
    db_session: Session,
    test_contractor: Contractor,
) -> None:
    """Estimate with single line item should work."""
    tools = create_estimate_tools(db_session, test_contractor)
    generate = tools[0].function

    result = await generate(
        description="Plumbing repair",
        line_items=[{"description": "Fix leaking pipe", "quantity": 1, "unit_price": 350.00}],
    )

    assert "$350.00" in result
    assert "1 line item" in result


@pytest.mark.asyncio()
async def test_generate_estimate_custom_terms(
    db_session: Session,
    test_contractor: Contractor,
) -> None:
    """Custom terms should be accepted."""
    tools = create_estimate_tools(db_session, test_contractor)
    generate = tools[0].function

    result = await generate(
        description="Fence install",
        line_items=[{"description": "Fence", "quantity": 1, "unit_price": 5000.00}],
        terms="50% upfront, 50% on completion",
    )

    assert "EST-0001" in result


def test_next_estimate_number_empty(
    db_session: Session,
    test_contractor: Contractor,
) -> None:
    """First estimate should be EST-0001."""
    assert _next_estimate_number(db_session, test_contractor.id) == "EST-0001"


def test_next_estimate_number_increments(
    db_session: Session,
    test_contractor: Contractor,
) -> None:
    """Estimate number should increment."""
    db_session.add(
        Estimate(
            contractor_id=test_contractor.id,
            description="Test",
            total_amount=100.0,
        )
    )
    db_session.commit()
    assert _next_estimate_number(db_session, test_contractor.id) == "EST-0002"


def test_serve_estimate_pdf_endpoint(client: TestClient) -> None:
    """GET /api/estimates/{id}/pdf should serve existing PDF."""
    # Create a test PDF file
    pdf_dir = Path("data/estimates")
    pdf_dir.mkdir(parents=True, exist_ok=True)
    test_pdf = pdf_dir / "999.pdf"
    test_pdf.write_bytes(b"%PDF-1.4 test content")

    try:
        response = client.get("/api/estimates/999/pdf")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/pdf"
        assert b"%PDF-1.4" in response.content
    finally:
        test_pdf.unlink()


def test_serve_estimate_pdf_not_found(client: TestClient) -> None:
    """GET /api/estimates/{id}/pdf should return 404 for missing PDF."""
    response = client.get("/api/estimates/99999/pdf")
    assert response.status_code == 404
