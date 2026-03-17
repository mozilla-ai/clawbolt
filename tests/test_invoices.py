from collections.abc import Generator
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import backend.app.database as _db_module
from backend.app.agent.client_db import EstimateStore, InvoiceStore
from backend.app.agent.tools.invoice_tools import create_invoice_tools
from backend.app.enums import EstimateStatus
from backend.app.models import User
from tests.mocks.storage import MockStorageBackend


@pytest.fixture(autouse=True)
def _use_tmp_pdf_dir(tmp_path: Path) -> Generator[None]:
    """Redirect PDF output to a temp directory so tests don't touch the real filesystem."""
    pdf_dir = tmp_path / "estimates"
    pdf_dir.mkdir()
    with (
        patch("backend.app.agent.tools.invoice_tools.PDF_BASE_DIR", pdf_dir),
        patch("backend.app.routers.invoices.PDF_BASE_DIR", pdf_dir),
    ):
        yield


# ---------------------------------------------------------------------------
# generate_invoice tool tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_generate_invoice_creates_records(
    test_user: User,
) -> None:
    """generate_invoice should create Invoice and InvoiceLineItem records."""
    tools = create_invoice_tools(test_user)
    generate = tools[0].function

    result = await generate(
        description="Kitchen remodel",
        line_items=[
            {"description": "Materials - cabinets", "quantity": 1, "unit_price": 3200.00},
            {"description": "Labor - install", "quantity": 16, "unit_price": 85.00},
        ],
    )

    assert "INV-0001" in result.content
    assert "$4,560.00" in result.content
    assert result.is_error is False

    store = InvoiceStore(test_user.id)
    invoices = await store.list_all()
    assert len(invoices) == 1
    assert invoices[0].total_amount == 4560.00
    assert invoices[0].status == "draft"
    assert invoices[0].description == "Kitchen remodel"
    assert len(invoices[0].line_items) == 2


@pytest.mark.asyncio()
async def test_generate_invoice_with_due_date(
    test_user: User,
) -> None:
    """generate_invoice should store due_date."""
    tools = create_invoice_tools(test_user)
    generate = tools[0].function

    result = await generate(
        description="Roof repair",
        line_items=[{"description": "Repair work", "quantity": 1, "unit_price": 2500.00}],
        due_date="2026-04-15",
    )

    assert "INV-0001" in result.content

    store = InvoiceStore(test_user.id)
    invoices = await store.list_all()
    assert invoices[0].due_date == "2026-04-15"


@pytest.mark.asyncio()
async def test_generate_invoice_with_client_info(
    test_user: User,
) -> None:
    """generate_invoice should include client info."""
    tools = create_invoice_tools(test_user)
    generate = tools[0].function

    result = await generate(
        description="Bathroom remodel",
        line_items=[{"description": "Full remodel", "quantity": 1, "unit_price": 8500.00}],
        client_name="Jane Doe",
        client_address="456 Elm St, Portland, OR",
    )

    assert "INV-0001" in result.content
    assert "$8,500.00" in result.content

    store = InvoiceStore(test_user.id)
    invoices = await store.list_all()
    assert invoices[0].client_id == "jane_doe"


@pytest.mark.asyncio()
async def test_generate_invoice_pdf_generated(
    test_user: User,
    tmp_path: Path,
) -> None:
    """generate_invoice should generate a PDF (nonzero bytes)."""
    tools = create_invoice_tools(test_user)
    generate = tools[0].function

    result = await generate(
        description="Quick fix",
        line_items=[{"description": "Service call", "quantity": 1, "unit_price": 150.00}],
    )

    assert ".pdf" in result.content

    store = InvoiceStore(test_user.id)
    invoices = await store.list_all()
    assert len(invoices) == 1
    pdf_path = tmp_path / "estimates" / str(test_user.id) / "unsorted" / f"{invoices[0].id}.pdf"
    assert pdf_path.exists()
    assert pdf_path.stat().st_size > 0


@pytest.mark.asyncio()
async def test_generate_invoice_sequential_numbers(
    test_user: User,
) -> None:
    """Invoice numbers should be sequential per user."""
    tools = create_invoice_tools(test_user)
    generate = tools[0].function

    result1 = await generate(
        description="Job 1",
        line_items=[{"description": "Work", "quantity": 1, "unit_price": 100.00}],
    )
    result2 = await generate(
        description="Job 2",
        line_items=[{"description": "Work", "quantity": 1, "unit_price": 200.00}],
    )

    assert "INV-0001" in result1.content
    assert "INV-0002" in result2.content


@pytest.mark.asyncio()
async def test_generate_invoice_rejects_negative_quantity(
    test_user: User,
) -> None:
    """Negative quantity should return an error."""
    tools = create_invoice_tools(test_user)
    generate = tools[0].function

    result = await generate(
        description="Bad invoice",
        line_items=[{"description": "Work", "quantity": -5, "unit_price": 100.00}],
    )

    assert "Error" in result.content
    assert "negative" in result.content.lower()
    assert result.is_error is True

    store = InvoiceStore(test_user.id)
    assert len(await store.list_all()) == 0


@pytest.mark.asyncio()
async def test_generate_invoice_rejects_negative_price(
    test_user: User,
) -> None:
    """Negative unit_price should return an error."""
    tools = create_invoice_tools(test_user)
    generate = tools[0].function

    result = await generate(
        description="Bad invoice",
        line_items=[{"description": "Work", "quantity": 1, "unit_price": -50.00}],
    )

    assert "Error" in result.content
    assert "negative" in result.content.lower()
    assert result.is_error is True

    store = InvoiceStore(test_user.id)
    assert len(await store.list_all()) == 0


@pytest.mark.asyncio()
async def test_generate_invoice_with_notes(
    test_user: User,
) -> None:
    """Notes should be stored on the invoice."""
    tools = create_invoice_tools(test_user)
    generate = tools[0].function

    await generate(
        description="Fence install",
        line_items=[{"description": "Fence", "quantity": 1, "unit_price": 5000.00}],
        notes="Payment due within 30 days",
    )

    store = InvoiceStore(test_user.id)
    invoices = await store.list_all()
    assert invoices[0].notes == "Payment due within 30 days"


# ---------------------------------------------------------------------------
# convert_estimate_to_invoice tool tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_convert_estimate_to_invoice(
    test_user: User,
) -> None:
    """convert_estimate_to_invoice should create invoice from accepted estimate."""
    # Create an accepted estimate first
    estimate_store = EstimateStore(test_user.id)
    estimate = await estimate_store.create(
        description="Deck build",
        total_amount=4200.00,
        status=EstimateStatus.ACCEPTED,
        line_items=[
            {"description": "Materials", "quantity": 1, "unit_price": 2400.00, "total": 2400.00},
            {"description": "Labor", "quantity": 24, "unit_price": 75.00, "total": 1800.00},
        ],
    )

    tools = create_invoice_tools(test_user)
    convert = tools[1].function

    result = await convert(estimate_id=estimate.id)

    assert "INV-0001" in result.content
    assert "$4,200.00" in result.content
    assert estimate.id in result.content
    assert result.is_error is False

    # Verify invoice was created with correct data
    invoice_store = InvoiceStore(test_user.id)
    invoices = await invoice_store.list_all()
    assert len(invoices) == 1
    assert invoices[0].estimate_id == estimate.id
    assert invoices[0].total_amount == 4200.00
    assert len(invoices[0].line_items) == 2


@pytest.mark.asyncio()
async def test_convert_estimate_to_invoice_not_found(
    test_user: User,
) -> None:
    """convert_estimate_to_invoice should error for non-existent estimate."""
    tools = create_invoice_tools(test_user)
    convert = tools[1].function

    result = await convert(estimate_id="EST-9999")

    assert result.is_error is True
    assert "not found" in result.content.lower()


@pytest.mark.asyncio()
async def test_convert_estimate_to_invoice_not_accepted(
    test_user: User,
) -> None:
    """convert_estimate_to_invoice should reject draft estimates."""
    estimate_store = EstimateStore(test_user.id)
    estimate = await estimate_store.create(
        description="Draft estimate",
        total_amount=1000.00,
        status=EstimateStatus.DRAFT,
        line_items=[
            {"description": "Work", "quantity": 1, "unit_price": 1000.00, "total": 1000.00},
        ],
    )

    tools = create_invoice_tools(test_user)
    convert = tools[1].function

    result = await convert(estimate_id=estimate.id)

    assert result.is_error is True
    assert "accepted" in result.content.lower()

    # Verify no invoice was created
    invoice_store = InvoiceStore(test_user.id)
    assert len(await invoice_store.list_all()) == 0


@pytest.mark.asyncio()
async def test_convert_estimate_to_invoice_with_due_date(
    test_user: User,
) -> None:
    """convert_estimate_to_invoice should accept due_date."""
    estimate_store = EstimateStore(test_user.id)
    estimate = await estimate_store.create(
        description="Plumbing",
        total_amount=500.00,
        status=EstimateStatus.ACCEPTED,
        line_items=[
            {"description": "Repair", "quantity": 1, "unit_price": 500.00, "total": 500.00},
        ],
    )

    tools = create_invoice_tools(test_user)
    convert = tools[1].function

    result = await convert(estimate_id=estimate.id, due_date="2026-04-01")
    assert result.is_error is False

    invoice_store = InvoiceStore(test_user.id)
    invoices = await invoice_store.list_all()
    assert invoices[0].due_date == "2026-04-01"


# ---------------------------------------------------------------------------
# InvoiceStore tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_invoice_store_create_and_get(
    test_user: User,
) -> None:
    """InvoiceStore should create and retrieve invoices."""
    store = InvoiceStore(test_user.id)

    invoice = await store.create(
        description="Test invoice",
        total_amount=1500.00,
        line_items=[
            {"description": "Item 1", "quantity": 2, "unit_price": 500.00, "total": 1000.00},
            {"description": "Item 2", "quantity": 1, "unit_price": 500.00, "total": 500.00},
        ],
    )

    assert invoice.id == "INV-0001"
    assert invoice.total_amount == 1500.00
    assert len(invoice.line_items) == 2

    retrieved = await store.get("INV-0001")
    assert retrieved is not None
    assert retrieved.description == "Test invoice"
    assert len(retrieved.line_items) == 2


@pytest.mark.asyncio()
async def test_invoice_store_update(
    test_user: User,
) -> None:
    """InvoiceStore.update should update fields."""
    store = InvoiceStore(test_user.id)
    await store.create(
        description="Original",
        total_amount=100.00,
    )

    updated = await store.update("INV-0001", status="paid")
    assert updated is not None
    assert updated.status == "paid"


@pytest.mark.asyncio()
async def test_invoice_store_list_all(
    test_user: User,
) -> None:
    """InvoiceStore.list_all should return all invoices for user."""
    store = InvoiceStore(test_user.id)
    await store.create(description="Invoice 1", total_amount=100.00)
    await store.create(description="Invoice 2", total_amount=200.00)

    invoices = await store.list_all()
    assert len(invoices) == 2
    assert invoices[0].id == "INV-0001"
    assert invoices[1].id == "INV-0002"


@pytest.mark.asyncio()
async def test_invoice_store_user_scoping(
    test_user: User,
) -> None:
    """InvoiceStore should not return invoices from other users."""
    # Create an invoice for the test user
    store = InvoiceStore(test_user.id)
    await store.create(description="My invoice", total_amount=100.00)

    # Create another user and their invoice
    db = _db_module.SessionLocal()
    try:
        other_user = User(user_id="other-user-999", phone="+15559999999")
        db.add(other_user)
        db.commit()
        db.refresh(other_user)
        db.expunge(other_user)
    finally:
        db.close()

    # Other user's invoice gets INV-0002 because IDs are globally unique PKs
    other_store = InvoiceStore(other_user.id)
    other_invoice = await other_store.create(description="Their invoice", total_amount=200.00)
    assert other_invoice.id == "INV-0002"

    # Each user should only see their own invoices
    my_invoices = await store.list_all()
    assert len(my_invoices) == 1
    assert my_invoices[0].description == "My invoice"

    their_invoices = await other_store.list_all()
    assert len(their_invoices) == 1
    assert their_invoices[0].description == "Their invoice"


# ---------------------------------------------------------------------------
# Invoice PDF endpoint tests
# ---------------------------------------------------------------------------


def test_serve_invoice_pdf_endpoint(client: TestClient, test_user: User, tmp_path: Path) -> None:
    """GET /api/invoices/{id}/pdf should serve existing PDF for authenticated owner."""
    import asyncio

    store = InvoiceStore(test_user.id)
    invoice = asyncio.get_event_loop().run_until_complete(
        store.create(
            description="Test invoice",
            total_amount=500.0,
        )
    )

    user_dir = tmp_path / "estimates" / str(test_user.id) / "unsorted"
    user_dir.mkdir(parents=True, exist_ok=True)
    test_pdf = user_dir / f"{invoice.id}.pdf"
    test_pdf.write_bytes(b"%PDF-1.4 test content")

    response = client.get(f"/api/invoices/{invoice.id}/pdf")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert b"%PDF-1.4" in response.content


def test_serve_invoice_pdf_not_found(client: TestClient) -> None:
    """GET /api/invoices/{id}/pdf should return 404 for missing invoice."""
    response = client.get("/api/invoices/99999/pdf")
    assert response.status_code == 404


def test_serve_invoice_pdf_other_user_rejected(client: TestClient, tmp_path: Path) -> None:
    """GET /api/invoices/{id}/pdf should return 404 for another user's invoice."""
    import asyncio

    db = _db_module.SessionLocal()
    try:
        other_user = User(user_id="other-user-999", phone="+15559999999")
        db.add(other_user)
        db.commit()
        db.refresh(other_user)
        db.expunge(other_user)
    finally:
        db.close()

    store = InvoiceStore(other_user.id)
    invoice = asyncio.get_event_loop().run_until_complete(
        store.create(
            description="Other user's invoice",
            total_amount=1000.0,
        )
    )

    user_dir = tmp_path / "estimates" / str(other_user.id) / "unsorted"
    user_dir.mkdir(parents=True, exist_ok=True)
    test_pdf = user_dir / f"{invoice.id}.pdf"
    test_pdf.write_bytes(b"%PDF-1.4 secret content")

    response = client.get(f"/api/invoices/{invoice.id}/pdf")
    assert response.status_code == 404
    assert response.json()["detail"] == "Invoice not found"


# ---------------------------------------------------------------------------
# Invoice PDF generation tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_generate_invoice_pdf_content() -> None:
    """generate_invoice_pdf should return valid PDF bytes."""
    from backend.app.services.pdf_service import InvoicePDFData, generate_invoice_pdf

    data = InvoicePDFData(
        owner_name="Test Contractor",
        owner_phone="+15551234567",
        owner_trade="General Contractor",
        description="Kitchen remodel",
        line_items=[
            {"description": "Cabinets", "quantity": 1, "unit_price": 3200.00, "total": 3200.00},
        ],
        subtotal=3200.00,
        total=3200.00,
        invoice_date="2026-03-13",
        invoice_number="INV-0001",
        client_name="Jane Smith",
        due_date="2026-04-13",
        notes="Net 30",
    )

    pdf_bytes = await generate_invoice_pdf(data)
    assert len(pdf_bytes) > 0
    assert pdf_bytes[:5] == b"%PDF-"


# ---------------------------------------------------------------------------
# Cloud storage tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_generate_invoice_uploads_to_cloud_storage(
    test_user: User,
) -> None:
    """When storage is provided, invoice PDF should be uploaded to cloud storage."""
    storage = MockStorageBackend()
    tools = create_invoice_tools(test_user, storage)
    generate = tools[0].function

    await generate(
        description="Deck build",
        line_items=[{"description": "Materials", "quantity": 1, "unit_price": 2000.00}],
        client_name="John Smith",
        client_address="116 Virginia Ave",
    )

    assert len(storage.files) == 1
    path = next(iter(storage.files))
    assert "/John Smith - 116 Virginia Ave/invoices/" in path
    assert "INV-0001.pdf" in path


@pytest.mark.asyncio()
async def test_generate_invoice_cloud_upload_failure_still_saves_locally(
    test_user: User,
    tmp_path: Path,
) -> None:
    """Cloud upload failure should not prevent local PDF generation."""
    storage = MockStorageBackend()

    async def failing_upload(content: bytes, folder: str, filename: str) -> str:
        raise RuntimeError("Simulated cloud failure")

    storage.upload_file = failing_upload  # type: ignore[assignment]

    tools = create_invoice_tools(test_user, storage)
    generate = tools[0].function

    result = await generate(
        description="Deck build",
        line_items=[{"description": "Materials", "quantity": 1, "unit_price": 2000.00}],
        client_name="John Smith",
    )

    assert "INV-0001" in result.content
    assert "$2,000.00" in result.content

    store = InvoiceStore(test_user.id)
    invoices = await store.list_all()
    assert len(invoices) == 1
    pdf_path = tmp_path / "estimates" / str(test_user.id) / "john_smith" / f"{invoices[0].id}.pdf"
    assert pdf_path.exists()


def test_serve_invoice_pdf_requires_auth_dependency() -> None:
    """The PDF endpoint must declare get_current_user as a dependency."""
    import inspect

    from backend.app.auth.dependencies import get_current_user
    from backend.app.routers.invoices import serve_invoice_pdf

    sig = inspect.signature(serve_invoice_pdf)
    dependencies = {
        p.default.dependency for p in sig.parameters.values() if hasattr(p.default, "dependency")
    }
    assert get_current_user in dependencies, "serve_invoice_pdf must use Depends(get_current_user)"
