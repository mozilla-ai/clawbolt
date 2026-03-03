from collections.abc import Generator
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from backend.app.agent.tools.estimate_tools import create_estimate_tools
from backend.app.models import Contractor, Estimate, EstimateLineItem
from tests.mocks.storage import MockStorageBackend


@pytest.fixture(autouse=True)
def _use_tmp_pdf_dir(tmp_path: Path) -> Generator[None]:
    """Redirect PDF output to a temp directory so tests don't touch the real filesystem."""
    pdf_dir = tmp_path / "estimates"
    pdf_dir.mkdir()
    with (
        patch("backend.app.agent.tools.estimate_tools.PDF_BASE_DIR", pdf_dir),
        patch("backend.app.routers.estimates.PDF_BASE_DIR", pdf_dir),
    ):
        yield


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

    assert "EST-0001" in result.content
    assert "$4,200.00" in result.content
    assert result.is_error is False

    estimate = (
        db_session.query(Estimate).filter(Estimate.contractor_id == test_contractor.id).first()
    )
    assert estimate is not None
    assert estimate.total_amount == 4200.00
    assert estimate.status == "draft"
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

    assert "EST-0001" in result.content
    assert "$8,500.00" in result.content


@pytest.mark.asyncio()
async def test_generate_estimate_pdf_generated(
    db_session: Session,
    test_contractor: Contractor,
    tmp_path: Path,
) -> None:
    """generate_estimate should generate a PDF (nonzero bytes)."""
    tools = create_estimate_tools(db_session, test_contractor)
    generate = tools[0].function

    result = await generate(
        description="Quick fix",
        line_items=[{"description": "Service call", "quantity": 1, "unit_price": 150.00}],
    )

    assert ".pdf" in result.content

    # Verify PDF file was actually written in the temp directory (per-contractor subdir)
    estimate = db_session.query(Estimate).first()
    assert estimate is not None
    pdf_path = tmp_path / "estimates" / str(test_contractor.id) / f"{estimate.id}.pdf"
    assert pdf_path.exists()
    assert pdf_path.stat().st_size > 0


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

    assert "EST-0001" in result1.content
    assert "EST-0002" in result2.content


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

    assert "$350.00" in result.content
    assert "1 line item" in result.content


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

    assert "EST-0001" in result.content


@pytest.mark.asyncio()
async def test_generate_estimate_rejects_negative_quantity(
    db_session: Session,
    test_contractor: Contractor,
) -> None:
    """Negative quantity should return an error instead of creating a record."""
    tools = create_estimate_tools(db_session, test_contractor)
    generate = tools[0].function

    result = await generate(
        description="Bad estimate",
        line_items=[{"description": "Work", "quantity": -5, "unit_price": 100.00}],
    )

    assert "Error" in result.content
    assert "negative" in result.content.lower()
    assert result.is_error is True
    assert db_session.query(Estimate).count() == 0


@pytest.mark.asyncio()
async def test_generate_estimate_rejects_negative_price(
    db_session: Session,
    test_contractor: Contractor,
) -> None:
    """Negative unit_price should return an error."""
    tools = create_estimate_tools(db_session, test_contractor)
    generate = tools[0].function

    result = await generate(
        description="Bad estimate",
        line_items=[{"description": "Work", "quantity": 1, "unit_price": -50.00}],
    )

    assert "Error" in result.content
    assert "negative" in result.content.lower()
    assert result.is_error is True
    assert db_session.query(Estimate).count() == 0


@pytest.mark.asyncio()
async def test_generate_estimate_rejects_non_numeric_values(
    db_session: Session,
    test_contractor: Contractor,
) -> None:
    """Non-numeric quantity/price should return an error."""
    tools = create_estimate_tools(db_session, test_contractor)
    generate = tools[0].function

    result = await generate(
        description="Bad estimate",
        line_items=[{"description": "Work", "quantity": "abc", "unit_price": 100.00}],
    )

    assert "Error" in result.content
    assert result.is_error is True
    assert db_session.query(Estimate).count() == 0


def test_serve_estimate_pdf_endpoint(
    client: TestClient, db_session: Session, test_contractor: Contractor, tmp_path: Path
) -> None:
    """GET /api/estimates/{id}/pdf should serve existing PDF for authenticated owner."""
    # Create an estimate record owned by the test contractor
    estimate = Estimate(
        contractor_id=test_contractor.id,
        description="Test estimate",
        total_amount=500.0,
    )
    db_session.add(estimate)
    db_session.commit()
    db_session.refresh(estimate)

    # Create a test PDF file in the temp directory (patched via _use_tmp_pdf_dir)
    contractor_dir = tmp_path / "estimates" / str(test_contractor.id)
    contractor_dir.mkdir(parents=True, exist_ok=True)
    test_pdf = contractor_dir / f"{estimate.id}.pdf"
    test_pdf.write_bytes(b"%PDF-1.4 test content")

    response = client.get(f"/api/estimates/{estimate.id}/pdf")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert b"%PDF-1.4" in response.content


def test_serve_estimate_pdf_not_found(client: TestClient) -> None:
    """GET /api/estimates/{id}/pdf should return 404 for missing estimate."""
    response = client.get("/api/estimates/99999/pdf")
    assert response.status_code == 404


def test_serve_estimate_pdf_other_user_rejected(
    client: TestClient, db_session: Session, tmp_path: Path
) -> None:
    """GET /api/estimates/{id}/pdf should return 404 for another user's estimate."""
    # Create a different contractor
    other_contractor = Contractor(
        user_id="other-user-999",
        name="Other Contractor",
        phone="+15559999999",
        trade="Electrician",
    )
    db_session.add(other_contractor)
    db_session.commit()
    db_session.refresh(other_contractor)

    # Create an estimate owned by the other contractor
    estimate = Estimate(
        contractor_id=other_contractor.id,
        description="Other user's estimate",
        total_amount=1000.0,
    )
    db_session.add(estimate)
    db_session.commit()
    db_session.refresh(estimate)

    # Create the PDF file so we can verify auth blocks access, not file absence
    contractor_dir = tmp_path / "estimates" / str(other_contractor.id)
    contractor_dir.mkdir(parents=True, exist_ok=True)
    test_pdf = contractor_dir / f"{estimate.id}.pdf"
    test_pdf.write_bytes(b"%PDF-1.4 secret content")

    response = client.get(f"/api/estimates/{estimate.id}/pdf")
    assert response.status_code == 404
    assert response.json()["detail"] == "Estimate not found"


@pytest.mark.asyncio()
async def test_generate_estimate_uploads_to_cloud_storage(
    db_session: Session,
    test_contractor: Contractor,
) -> None:
    """When storage is provided, estimate PDF should be uploaded to cloud storage."""
    storage = MockStorageBackend()
    tools = create_estimate_tools(db_session, test_contractor, storage)
    generate = tools[0].function

    await generate(
        description="Deck build",
        line_items=[{"description": "Materials", "quantity": 1, "unit_price": 2000.00}],
        client_name="John Smith",
        client_address="116 Virginia Ave",
    )

    # PDF should be uploaded to client folder in storage
    assert len(storage.files) == 1
    path = next(iter(storage.files))
    assert "/John Smith - 116 Virginia Ave/estimates/" in path
    assert "EST-0001.pdf" in path


@pytest.mark.asyncio()
async def test_generate_estimate_storage_uses_unsorted_without_client(
    db_session: Session,
    test_contractor: Contractor,
) -> None:
    """Without client info, estimate PDF should go to Unsorted in cloud storage."""
    storage = MockStorageBackend()
    tools = create_estimate_tools(db_session, test_contractor, storage)
    generate = tools[0].function

    await generate(
        description="Quick repair",
        line_items=[{"description": "Labor", "quantity": 1, "unit_price": 150.00}],
    )

    assert len(storage.files) == 1
    path = next(iter(storage.files))
    assert "/Unsorted/" in path


@pytest.mark.asyncio()
async def test_generate_estimate_no_storage_still_saves_locally(
    db_session: Session,
    test_contractor: Contractor,
    tmp_path: Path,
) -> None:
    """Without storage backend, estimate PDF should still save to local filesystem."""
    tools = create_estimate_tools(db_session, test_contractor)
    generate = tools[0].function

    result = await generate(
        description="Local only",
        line_items=[{"description": "Work", "quantity": 1, "unit_price": 100.00}],
    )

    assert "EST-0001" in result.content
    estimate = db_session.query(Estimate).first()
    assert estimate is not None
    pdf_path = tmp_path / "estimates" / str(test_contractor.id) / f"{estimate.id}.pdf"
    assert pdf_path.exists()


def test_serve_estimate_pdf_requires_auth_dependency() -> None:
    """The PDF endpoint must declare get_current_user as a dependency."""
    import inspect

    from backend.app.auth.dependencies import get_current_user
    from backend.app.routers.estimates import serve_estimate_pdf

    sig = inspect.signature(serve_estimate_pdf)
    dependencies = {
        p.default.dependency for p in sig.parameters.values() if hasattr(p.default, "dependency")
    }
    assert get_current_user in dependencies, "serve_estimate_pdf must use Depends(get_current_user)"


@pytest.mark.asyncio()
async def test_generate_estimate_cloud_upload_failure_does_not_kill_call(
    db_session: Session,
    test_contractor: Contractor,
    tmp_path: Path,
) -> None:
    """Cloud upload failure should be logged but not prevent local PDF generation."""
    storage = MockStorageBackend()

    # Make upload_file raise to simulate a cloud failure
    async def failing_upload(content: bytes, folder: str, filename: str) -> str:
        raise RuntimeError("Simulated cloud failure")

    storage.upload_file = failing_upload  # type: ignore[assignment]

    tools = create_estimate_tools(db_session, test_contractor, storage)
    generate = tools[0].function

    result = await generate(
        description="Deck build",
        line_items=[{"description": "Materials", "quantity": 1, "unit_price": 2000.00}],
        client_name="John Smith",
    )

    # The estimate should still succeed with the local PDF
    assert "EST-0001" in result.content
    assert "$2,000.00" in result.content

    # Verify local PDF was saved
    estimate = db_session.query(Estimate).first()
    assert estimate is not None
    pdf_path = tmp_path / "estimates" / str(test_contractor.id) / f"{estimate.id}.pdf"
    assert pdf_path.exists()
