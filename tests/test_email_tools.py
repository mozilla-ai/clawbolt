"""Tests for the send_document_email tool."""

from __future__ import annotations

import uuid
from collections.abc import Generator
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

import backend.app.database as _db_module
from backend.app.agent.client_db import EstimateStore, InvoiceStore
from backend.app.agent.tools.email_tools import create_email_tools
from backend.app.enums import EstimateStatus, InvoiceStatus
from backend.app.models import User
from backend.app.services.email_service import EmailResult


@pytest.fixture(autouse=True)
def _use_tmp_pdf_dir(tmp_path: Path) -> Generator[None]:
    """Redirect PDF output to a temp directory."""
    pdf_dir = tmp_path / "estimates"
    pdf_dir.mkdir()
    with patch("backend.app.agent.tools.email_tools.PDF_BASE_DIR", pdf_dir):
        yield


def _write_test_pdf(tmp_path: Path, user_id: str, doc_id: str, subfolder: str = "unsorted") -> Path:
    """Write a fake PDF file and return its path."""
    pdf_dir = tmp_path / "estimates" / str(user_id) / subfolder
    pdf_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = pdf_dir / f"{doc_id}.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 test content")
    return pdf_path


# ---------------------------------------------------------------------------
# send_document_email tool tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_send_estimate_email(test_user: User, tmp_path: Path) -> None:
    """send_document_email should send an estimate PDF via email."""
    # Create an estimate with a PDF
    store = EstimateStore(test_user.id)
    estimate = await store.create(
        description="Deck build",
        total_amount=4200.00,
        line_items=[
            {"description": "Materials", "quantity": 1, "unit_price": 2400.00, "total": 2400.00},
        ],
    )
    pdf_path = _write_test_pdf(tmp_path, test_user.id, estimate.id)
    await store.update(estimate.id, pdf_url=str(pdf_path))

    mock_email_service = AsyncMock()
    mock_email_service.send_email.return_value = EmailResult(success=True, message_id="msg-123")

    with (
        patch("backend.app.agent.tools.email_tools.settings.email_provider", "resend"),
        patch(
            "backend.app.agent.tools.email_tools.get_email_service",
            return_value=mock_email_service,
        ),
    ):
        tools = create_email_tools(test_user)
        send = tools[0].function

        result = await send(
            recipient_email="client@example.com",
            document_type="estimate",
            document_id=estimate.id,
        )

    assert result.is_error is False
    assert "sent to client@example.com" in result.content

    # Verify email was sent with attachment
    mock_email_service.send_email.assert_called_once()
    call_kwargs = mock_email_service.send_email.call_args.kwargs
    assert call_kwargs["to"] == "client@example.com"
    assert len(call_kwargs["attachments"]) == 1
    assert call_kwargs["attachments"][0].filename == f"{estimate.id}.pdf"

    # Verify estimate status was updated to sent
    updated = await store.get(estimate.id)
    assert updated is not None
    assert updated.status == EstimateStatus.SENT


@pytest.mark.asyncio()
async def test_send_invoice_email(test_user: User, tmp_path: Path) -> None:
    """send_document_email should send an invoice PDF via email."""
    store = InvoiceStore(test_user.id)
    invoice = await store.create(
        description="Kitchen remodel",
        total_amount=5000.00,
    )
    pdf_path = _write_test_pdf(tmp_path, test_user.id, invoice.id)
    await store.update(invoice.id, pdf_url=str(pdf_path))

    mock_email_service = AsyncMock()
    mock_email_service.send_email.return_value = EmailResult(success=True, message_id="msg-456")

    with (
        patch("backend.app.agent.tools.email_tools.settings.email_provider", "resend"),
        patch(
            "backend.app.agent.tools.email_tools.get_email_service",
            return_value=mock_email_service,
        ),
    ):
        tools = create_email_tools(test_user)
        send = tools[0].function

        result = await send(
            recipient_email="client@example.com",
            document_type="invoice",
            document_id=invoice.id,
        )

    assert result.is_error is False
    assert "sent to client@example.com" in result.content

    # Verify invoice status was updated to sent
    updated = await store.get(invoice.id)
    assert updated is not None
    assert updated.status == InvoiceStatus.SENT


@pytest.mark.asyncio()
async def test_send_email_custom_subject_body(test_user: User, tmp_path: Path) -> None:
    """send_document_email should use custom subject and body when provided."""
    store = InvoiceStore(test_user.id)
    invoice = await store.create(description="Test", total_amount=100.00)
    pdf_path = _write_test_pdf(tmp_path, test_user.id, invoice.id)
    await store.update(invoice.id, pdf_url=str(pdf_path))

    mock_email_service = AsyncMock()
    mock_email_service.send_email.return_value = EmailResult(success=True)

    with (
        patch("backend.app.agent.tools.email_tools.settings.email_provider", "resend"),
        patch(
            "backend.app.agent.tools.email_tools.get_email_service",
            return_value=mock_email_service,
        ),
    ):
        tools = create_email_tools(test_user)
        send = tools[0].function

        await send(
            recipient_email="client@example.com",
            document_type="invoice",
            document_id=invoice.id,
            subject="Custom subject",
            body="Custom body text",
        )

    call_kwargs = mock_email_service.send_email.call_args.kwargs
    assert call_kwargs["subject"] == "Custom subject"
    assert call_kwargs["body_text"] == "Custom body text"


@pytest.mark.asyncio()
async def test_send_email_not_configured(test_user: User) -> None:
    """send_document_email should error when email is not configured."""
    with patch("backend.app.agent.tools.email_tools.settings.email_provider", ""):
        tools = create_email_tools(test_user)
        send = tools[0].function

        result = await send(
            recipient_email="client@example.com",
            document_type="estimate",
            document_id="EST-0001",
        )

    assert result.is_error is True
    assert "not configured" in result.content.lower()


@pytest.mark.asyncio()
async def test_send_email_document_not_found(test_user: User) -> None:
    """send_document_email should error when document doesn't exist."""
    mock_email_service = AsyncMock()

    with (
        patch("backend.app.agent.tools.email_tools.settings.email_provider", "resend"),
        patch(
            "backend.app.agent.tools.email_tools.get_email_service",
            return_value=mock_email_service,
        ),
    ):
        tools = create_email_tools(test_user)
        send = tools[0].function

        result = await send(
            recipient_email="client@example.com",
            document_type="estimate",
            document_id="EST-9999",
        )

    assert result.is_error is True
    assert "not found" in result.content.lower()


@pytest.mark.asyncio()
async def test_send_email_no_pdf_generated(test_user: User) -> None:
    """send_document_email should error when document has no PDF."""
    store = InvoiceStore(test_user.id)
    invoice = await store.create(description="No PDF", total_amount=100.00)
    # Don't generate a PDF

    mock_email_service = AsyncMock()

    with (
        patch("backend.app.agent.tools.email_tools.settings.email_provider", "resend"),
        patch(
            "backend.app.agent.tools.email_tools.get_email_service",
            return_value=mock_email_service,
        ),
    ):
        tools = create_email_tools(test_user)
        send = tools[0].function

        result = await send(
            recipient_email="client@example.com",
            document_type="invoice",
            document_id=invoice.id,
        )

    assert result.is_error is True
    assert "pdf" in result.content.lower()


@pytest.mark.asyncio()
async def test_send_email_delivery_failure(test_user: User, tmp_path: Path) -> None:
    """send_document_email should report email delivery failure."""
    store = EstimateStore(test_user.id)
    estimate = await store.create(description="Test", total_amount=100.00)
    pdf_path = _write_test_pdf(tmp_path, test_user.id, estimate.id)
    await store.update(estimate.id, pdf_url=str(pdf_path))

    mock_email_service = AsyncMock()
    mock_email_service.send_email.return_value = EmailResult(
        success=False, error="Invalid recipient"
    )

    with (
        patch("backend.app.agent.tools.email_tools.settings.email_provider", "resend"),
        patch(
            "backend.app.agent.tools.email_tools.get_email_service",
            return_value=mock_email_service,
        ),
    ):
        tools = create_email_tools(test_user)
        send = tools[0].function

        result = await send(
            recipient_email="bad@email",
            document_type="estimate",
            document_id=estimate.id,
        )

    assert result.is_error is True
    assert "Invalid recipient" in result.content

    # Status should NOT be updated on failure
    updated = await store.get(estimate.id)
    assert updated is not None
    assert updated.status == EstimateStatus.DRAFT


@pytest.mark.asyncio()
async def test_send_email_does_not_update_non_draft_status(test_user: User, tmp_path: Path) -> None:
    """send_document_email should not update status if already sent/accepted."""
    store = EstimateStore(test_user.id)
    estimate = await store.create(
        description="Test",
        total_amount=100.00,
        status=EstimateStatus.ACCEPTED,
    )
    pdf_path = _write_test_pdf(tmp_path, test_user.id, estimate.id)
    await store.update(estimate.id, pdf_url=str(pdf_path))

    mock_email_service = AsyncMock()
    mock_email_service.send_email.return_value = EmailResult(success=True)

    with (
        patch("backend.app.agent.tools.email_tools.settings.email_provider", "resend"),
        patch(
            "backend.app.agent.tools.email_tools.get_email_service",
            return_value=mock_email_service,
        ),
    ):
        tools = create_email_tools(test_user)
        send = tools[0].function

        result = await send(
            recipient_email="client@example.com",
            document_type="estimate",
            document_id=estimate.id,
        )

    assert result.is_error is False
    # Status should remain "accepted", not be overwritten to "sent"
    updated = await store.get(estimate.id)
    assert updated is not None
    assert updated.status == EstimateStatus.ACCEPTED


# ---------------------------------------------------------------------------
# User scoping tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_send_email_user_scoping(test_user: User, tmp_path: Path) -> None:
    """User A should not be able to send User B's documents via email."""
    # Create a second user
    db = _db_module.SessionLocal()
    try:
        other_user = User(
            id=str(uuid.uuid4()),
            user_id="other-user-002",
            phone="+15559999999",
            onboarding_complete=True,
        )
        db.add(other_user)
        db.commit()
        db.refresh(other_user)
        db.expunge(other_user)
    finally:
        db.close()

    # Create an invoice owned by other_user
    other_store = InvoiceStore(other_user.id)
    other_invoice = await other_store.create(
        description="Other user invoice",
        total_amount=1000.00,
    )
    # Write a PDF for it so the only gate is ownership
    pdf_path = _write_test_pdf(tmp_path, other_user.id, other_invoice.id)
    await other_store.update(other_invoice.id, pdf_url=str(pdf_path))

    mock_email_service = AsyncMock()
    mock_email_service.send_email.return_value = EmailResult(success=True)

    with (
        patch("backend.app.agent.tools.email_tools.settings.email_provider", "resend"),
        patch(
            "backend.app.agent.tools.email_tools.get_email_service",
            return_value=mock_email_service,
        ),
    ):
        # Create tools scoped to test_user (User A)
        tools = create_email_tools(test_user)
        send = tools[0].function

        # Try to send other_user's (User B) invoice
        result = await send(
            recipient_email="client@example.com",
            document_type="invoice",
            document_id=other_invoice.id,
        )

    # Should fail because test_user doesn't own this invoice
    assert result.is_error is True
    assert "not found" in result.content.lower()

    # Email should NOT have been sent
    mock_email_service.send_email.assert_not_called()
