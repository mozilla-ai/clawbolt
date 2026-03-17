"""Tests for the email service implementations."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.services.email_service import (
    EmailAttachment,
    ResendEmailService,
    SMTPEmailService,
    get_email_service,
)

# ---------------------------------------------------------------------------
# ResendEmailService tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_resend_send_email_success() -> None:
    """ResendEmailService should send email via Resend API."""
    service = ResendEmailService(
        api_key="re_test_123",
        from_address="test@example.com",
        from_name="Test Contractor",
    )

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"id": "msg-123"}

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = await service.send_email(
            to="client@example.com",
            subject="Invoice INV-0001",
            body_text="Please find your invoice attached.",
        )

    assert result.success is True
    assert result.message_id == "msg-123"


@pytest.mark.asyncio()
async def test_resend_send_email_with_attachment() -> None:
    """ResendEmailService should include attachments in the API call."""
    service = ResendEmailService(
        api_key="re_test_123",
        from_address="test@example.com",
    )

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"id": "msg-456"}

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = await service.send_email(
            to="client@example.com",
            subject="Estimate",
            body_text="Attached.",
            attachments=[
                EmailAttachment(
                    filename="EST-0001.pdf",
                    content=b"%PDF-fake",
                    content_type="application/pdf",
                )
            ],
        )

    assert result.success is True

    # Verify the payload included attachments
    call_kwargs = mock_client.post.call_args
    payload = call_kwargs.kwargs["json"]
    assert "attachments" in payload
    assert len(payload["attachments"]) == 1
    assert payload["attachments"][0]["filename"] == "EST-0001.pdf"


@pytest.mark.asyncio()
async def test_resend_send_email_api_error() -> None:
    """ResendEmailService should return error on API failure."""
    service = ResendEmailService(
        api_key="re_test_123",
        from_address="test@example.com",
    )

    mock_response = MagicMock()
    mock_response.status_code = 422
    mock_response.text = "Invalid email address"

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = await service.send_email(
            to="bad-email",
            subject="Test",
            body_text="Test",
        )

    assert result.success is False
    assert "422" in result.error


@pytest.mark.asyncio()
async def test_resend_send_email_network_error() -> None:
    """ResendEmailService should handle network errors gracefully."""
    service = ResendEmailService(
        api_key="re_test_123",
        from_address="test@example.com",
    )

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post.side_effect = Exception("Connection refused")
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = await service.send_email(
            to="client@example.com",
            subject="Test",
            body_text="Test",
        )

    assert result.success is False
    assert "Connection refused" in result.error


@pytest.mark.asyncio()
async def test_resend_from_header_with_name() -> None:
    """ResendEmailService should format from header with name."""
    service = ResendEmailService(
        api_key="re_test_123",
        from_address="test@example.com",
        from_name="John's Plumbing",
    )
    assert service._from_header() == "John's Plumbing <test@example.com>"


@pytest.mark.asyncio()
async def test_resend_from_header_without_name() -> None:
    """ResendEmailService should use just address when no name."""
    service = ResendEmailService(
        api_key="re_test_123",
        from_address="test@example.com",
    )
    assert service._from_header() == "test@example.com"


# ---------------------------------------------------------------------------
# SMTPEmailService tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_smtp_send_email_success() -> None:
    """SMTPEmailService should send email via aiosmtplib."""
    service = SMTPEmailService(
        host="smtp.example.com",
        port=587,
        username="user@example.com",
        password="secret",
        from_address="user@example.com",
        from_name="Test User",
    )

    with patch("aiosmtplib.send", new_callable=AsyncMock) as mock_send:
        result = await service.send_email(
            to="client@example.com",
            subject="Test Invoice",
            body_text="Please find attached.",
        )

    assert result.success is True
    mock_send.assert_called_once()


@pytest.mark.asyncio()
async def test_smtp_send_email_with_attachment() -> None:
    """SMTPEmailService should include attachments in the MIME message."""
    service = SMTPEmailService(
        host="smtp.example.com",
        port=587,
        username="user@example.com",
        password="secret",
        from_address="user@example.com",
    )

    with patch("aiosmtplib.send", new_callable=AsyncMock) as mock_send:
        result = await service.send_email(
            to="client@example.com",
            subject="Estimate",
            body_text="Attached.",
            attachments=[
                EmailAttachment(
                    filename="EST-0001.pdf",
                    content=b"%PDF-fake",
                    content_type="application/pdf",
                )
            ],
        )

    assert result.success is True
    # Verify the message was sent with the right structure
    sent_msg = mock_send.call_args[0][0]
    payloads = sent_msg.get_payload()
    # Should have text + attachment = at least 2 parts
    assert len(payloads) >= 2


@pytest.mark.asyncio()
async def test_smtp_send_email_failure() -> None:
    """SMTPEmailService should handle SMTP errors gracefully."""
    service = SMTPEmailService(
        host="smtp.example.com",
        port=587,
        username="user@example.com",
        password="secret",
        from_address="user@example.com",
    )

    with patch("aiosmtplib.send", new_callable=AsyncMock, side_effect=Exception("SMTP error")):
        result = await service.send_email(
            to="client@example.com",
            subject="Test",
            body_text="Test",
        )

    assert result.success is False
    assert "SMTP error" in result.error


# ---------------------------------------------------------------------------
# get_email_service factory tests
# ---------------------------------------------------------------------------


def test_get_email_service_resend() -> None:
    """get_email_service should return ResendEmailService when configured."""
    from backend.app.config import Settings

    s = Settings(
        email_provider="resend",
        resend_api_key="re_test_123",
        email_from_address="test@example.com",
        email_from_name="Test",
    )
    service = get_email_service(s)
    assert isinstance(service, ResendEmailService)


def test_get_email_service_smtp() -> None:
    """get_email_service should return SMTPEmailService when configured."""
    from backend.app.config import Settings

    s = Settings(
        email_provider="smtp",
        smtp_host="smtp.example.com",
        email_from_address="test@example.com",
    )
    service = get_email_service(s)
    assert isinstance(service, SMTPEmailService)


def test_get_email_service_none_when_not_configured() -> None:
    """get_email_service should return None when email_provider is empty."""
    from backend.app.config import Settings

    s = Settings(email_provider="")
    assert get_email_service(s) is None


def test_get_email_service_resend_missing_api_key() -> None:
    """get_email_service should return None when resend API key is missing."""
    from backend.app.config import Settings

    s = Settings(
        email_provider="resend",
        resend_api_key="",
        email_from_address="test@example.com",
    )
    assert get_email_service(s) is None


def test_get_email_service_smtp_missing_host() -> None:
    """get_email_service should return None when SMTP host is missing."""
    from backend.app.config import Settings

    s = Settings(
        email_provider="smtp",
        smtp_host="",
        email_from_address="test@example.com",
    )
    assert get_email_service(s) is None
