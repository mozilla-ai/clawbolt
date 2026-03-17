"""Abstract email service with Resend and SMTP implementations."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from backend.app.config import Settings

logger = logging.getLogger(__name__)


@dataclass
class EmailAttachment:
    """An email attachment."""

    filename: str
    content: bytes
    content_type: str = "application/octet-stream"


@dataclass
class EmailResult:
    """Result of sending an email."""

    success: bool
    message_id: str = ""
    error: str = ""


class EmailService(ABC):
    """Abstract base class for email sending."""

    @abstractmethod
    async def send_email(
        self,
        to: str,
        subject: str,
        body_text: str,
        body_html: str | None = None,
        attachments: list[EmailAttachment] | None = None,
    ) -> EmailResult:
        """Send an email. Returns EmailResult with success/failure info."""


class ResendEmailService(EmailService):
    """Email service using Resend API (httpx POST, no SDK needed)."""

    def __init__(self, api_key: str, from_address: str, from_name: str = "") -> None:
        self.api_key = api_key
        self.from_address = from_address
        self.from_name = from_name

    def _from_header(self) -> str:
        if self.from_name:
            return f"{self.from_name} <{self.from_address}>"
        return self.from_address

    async def send_email(
        self,
        to: str,
        subject: str,
        body_text: str,
        body_html: str | None = None,
        attachments: list[EmailAttachment] | None = None,
    ) -> EmailResult:
        """Send email via Resend API."""
        import base64

        payload: dict[str, object] = {
            "from": self._from_header(),
            "to": [to],
            "subject": subject,
            "text": body_text,
        }
        if body_html:
            payload["html"] = body_html

        if attachments:
            payload["attachments"] = [
                {
                    "filename": att.filename,
                    "content": base64.b64encode(att.content).decode("ascii"),
                    "content_type": att.content_type,
                }
                for att in attachments
            ]

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    "https://api.resend.com/emails",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=30.0,
                )
                if 200 <= resp.status_code < 300:
                    data = resp.json()
                    return EmailResult(success=True, message_id=data.get("id", ""))
                return EmailResult(
                    success=False,
                    error=f"Resend API error {resp.status_code}: {resp.text}",
                )
        except Exception as exc:
            logger.exception("Failed to send email via Resend")
            return EmailResult(success=False, error=str(exc))


class SMTPEmailService(EmailService):
    """Email service using SMTP via aiosmtplib."""

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        from_address: str,
        from_name: str = "",
        use_tls: bool = True,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.from_address = from_address
        self.from_name = from_name
        self.use_tls = use_tls

    def _from_header(self) -> str:
        if self.from_name:
            return f"{self.from_name} <{self.from_address}>"
        return self.from_address

    async def send_email(
        self,
        to: str,
        subject: str,
        body_text: str,
        body_html: str | None = None,
        attachments: list[EmailAttachment] | None = None,
    ) -> EmailResult:
        """Send email via SMTP."""
        import aiosmtplib

        # Use "alternative" so email clients pick text or HTML, not both
        msg = MIMEMultipart("alternative") if body_html else MIMEMultipart()
        msg["From"] = self._from_header()
        msg["To"] = to
        msg["Subject"] = subject

        msg.attach(MIMEText(body_text, "plain"))
        if body_html:
            msg.attach(MIMEText(body_html, "html"))

        for att in attachments or []:
            part = MIMEApplication(att.content)
            part.add_header("Content-Disposition", "attachment", filename=att.filename)
            if att.content_type:
                part.set_type(att.content_type)
            msg.attach(part)

        try:
            await aiosmtplib.send(
                msg,
                hostname=self.host,
                port=self.port,
                username=self.username,
                password=self.password,
                start_tls=self.use_tls,
            )
            return EmailResult(success=True)
        except Exception as exc:
            logger.exception("Failed to send email via SMTP")
            return EmailResult(success=False, error=str(exc))


def get_email_service(s: Settings) -> EmailService | None:
    """Create an EmailService based on settings. Returns None if not configured."""
    if s.email_provider == "resend":
        if not s.resend_api_key:
            logger.warning("email_provider=resend but resend_api_key is not set")
            return None
        if not s.email_from_address:
            logger.warning("email_provider=resend but email_from_address is not set")
            return None
        return ResendEmailService(
            api_key=s.resend_api_key,
            from_address=s.email_from_address,
            from_name=s.email_from_name,
        )

    if s.email_provider == "smtp":
        if not s.smtp_host:
            logger.warning("email_provider=smtp but smtp_host is not set")
            return None
        if not s.email_from_address:
            logger.warning("email_provider=smtp but email_from_address is not set")
            return None
        return SMTPEmailService(
            host=s.smtp_host,
            port=s.smtp_port,
            username=s.smtp_username,
            password=s.smtp_password,
            from_address=s.email_from_address,
            from_name=s.email_from_name,
            use_tls=s.smtp_use_tls,
        )

    return None
