"""Email sending tools for the agent."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from backend.app.agent.client_db import EstimateStore, InvoiceStore
from backend.app.agent.tools.base import Tool, ToolErrorKind, ToolResult
from backend.app.agent.tools.names import ToolName
from backend.app.config import settings
from backend.app.enums import EstimateStatus, InvoiceStatus
from backend.app.models import User
from backend.app.services.email_service import EmailAttachment, get_email_service

if TYPE_CHECKING:
    from backend.app.agent.tools.registry import ToolContext

PDF_BASE_DIR = Path(settings.pdf_storage_dir)

logger = logging.getLogger(__name__)


class SendDocumentEmailParams(BaseModel):
    """Parameters for the send_document_email tool."""

    recipient_email: str = Field(
        description="Email address to send to",
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
    )
    document_type: Literal["estimate", "invoice"] = Field(description="Type of document to send")
    document_id: str = Field(description="Document ID (e.g. EST-0001 or INV-0001)")
    subject: str | None = Field(
        default=None, description="Email subject (optional, auto-generated)"
    )
    body: str | None = Field(default=None, description="Email body text (optional, auto-generated)")


def create_email_tools(user: User) -> list[Tool]:
    """Create email-related tools for the agent."""

    async def send_document_email(
        recipient_email: str,
        document_type: str,
        document_id: str,
        subject: str | None = None,
        body: str | None = None,
    ) -> ToolResult:
        """Send an estimate or invoice PDF via email."""
        email_service = get_email_service(settings)
        if email_service is None:
            return ToolResult(
                content="Error: Email is not configured. Set EMAIL_PROVIDER and related settings.",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )

        # Load the document
        if document_type == "estimate":
            store = EstimateStore(user.id)
            doc = await store.get(document_id)
            if not doc:
                return ToolResult(
                    content=f"Error: Estimate {document_id} not found.",
                    is_error=True,
                    error_kind=ToolErrorKind.NOT_FOUND,
                )
            doc_label = f"Estimate {document_id}"
        elif document_type == "invoice":
            inv_store = InvoiceStore(user.id)
            doc = await inv_store.get(document_id)
            if not doc:
                return ToolResult(
                    content=f"Error: Invoice {document_id} not found.",
                    is_error=True,
                    error_kind=ToolErrorKind.NOT_FOUND,
                )
            doc_label = f"Invoice {document_id}"
        else:
            return ToolResult(
                content=f"Error: Invalid document_type '{document_type}'. Use 'estimate' or 'invoice'.",
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
            )

        # Read the PDF
        pdf_path = doc.pdf_url
        if not pdf_path:
            return ToolResult(
                content=f"Error: {doc_label} does not have a PDF generated yet.",
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
            )

        pdf_file = Path(pdf_path).resolve()
        # Verify the resolved path is under the expected PDF storage directory
        if not pdf_file.is_relative_to(PDF_BASE_DIR.resolve()):
            return ToolResult(
                content="Error: PDF path is outside allowed storage directory.",
                is_error=True,
                error_kind=ToolErrorKind.VALIDATION,
            )
        if not pdf_file.exists():
            return ToolResult(
                content=f"Error: PDF file not found for {doc_label}.",
                is_error=True,
                error_kind=ToolErrorKind.NOT_FOUND,
            )

        pdf_bytes = await asyncio.to_thread(pdf_file.read_bytes)

        # Build default subject/body if not provided
        owner_name = user.soul_text.split("\n")[0] if user.soul_text else "Your contractor"
        if not subject:
            subject = f"{doc_label} from {owner_name}"
        if not body:
            body = (
                f"Please find the attached {doc_label.lower()}.\n\n"
                f"Thank you for your business.\n\n"
                f"{owner_name}"
            )

        # Send email
        result = await email_service.send_email(
            to=recipient_email,
            subject=subject,
            body_text=body,
            attachments=[
                EmailAttachment(
                    filename=f"{document_id}.pdf",
                    content=pdf_bytes,
                    content_type="application/pdf",
                )
            ],
        )

        if not result.success:
            return ToolResult(
                content=f"Error sending email: {result.error}",
                is_error=True,
                error_kind=ToolErrorKind.SERVICE,
            )

        # Update document status to "sent" if still draft
        status_updated = False
        if document_type == "estimate":
            est = await store.get(document_id)
            if est and est.status == EstimateStatus.DRAFT:
                await store.update(document_id, status=EstimateStatus.SENT)
                status_updated = True
        elif document_type == "invoice":
            inv = await inv_store.get(document_id)
            if inv and inv.status == InvoiceStatus.DRAFT:
                await inv_store.update(document_id, status=InvoiceStatus.SENT)
                status_updated = True

        status_msg = " Document status updated to 'sent'." if status_updated else ""
        return ToolResult(content=f"{doc_label} sent to {recipient_email}.{status_msg}")

    return [
        Tool(
            name=ToolName.SEND_DOCUMENT_EMAIL,
            description=(
                "Send an estimate or invoice PDF to a client via email. "
                "Requires the document to have a generated PDF. "
                "The document status will be updated to 'sent' after successful delivery."
            ),
            function=send_document_email,
            params_model=SendDocumentEmailParams,
            usage_hint=(
                "Before calling this tool, confirm the recipient email address with the user. "
                "The document must already have a PDF generated."
            ),
        ),
    ]


def _email_factory(ctx: ToolContext) -> list[Tool]:
    """Factory for email tools, used by the registry.

    Only returns tools if email is configured.
    """
    if not settings.email_provider:
        return []
    return create_email_tools(ctx.user)


def _register() -> None:
    from backend.app.agent.tools.registry import default_registry

    default_registry.register(
        "email",
        _email_factory,
        core=False,
        summary="Send estimates and invoices to clients via email",
    )


_register()
