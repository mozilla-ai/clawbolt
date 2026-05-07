"""Compliance document upload tool for AppFolio Vendor Portal.

PMs ask vendors to upload W-9s, certificates of insurance, and licenses.
AppFolio's compliance endpoint takes a singular ``file`` field rather
than a list, so this tool resolves exactly one media reference.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from backend.app.agent.approval import ApprovalPolicy, PermissionLevel
from backend.app.agent.tools.base import Tool, ToolErrorKind, ToolReceipt, ToolResult
from backend.app.agent.tools.names import ToolName
from backend.app.integrations.appfolio_vendor.errors import service_error_to_tool_result
from backend.app.integrations.appfolio_vendor.media_resolver import resolve_staged_files
from backend.app.integrations.appfolio_vendor.params import (
    AppFolioUploadComplianceDocParams,
)
from backend.app.integrations.appfolio_vendor.service import AppFolioVendorService

if TYPE_CHECKING:
    from backend.app.agent.tools.registry import ToolContext

logger = logging.getLogger(__name__)


def build_compliance_tools(service: AppFolioVendorService, ctx: ToolContext) -> list[Tool]:
    """Return the AppFolio compliance-document tools."""

    async def appfolio_upload_compliance_doc(
        customer_id: str, compliance_type: str, media_ref: str
    ) -> ToolResult:
        files_or_err = await resolve_staged_files(ctx, [media_ref])
        if isinstance(files_or_err, ToolResult):
            return files_or_err
        if not files_or_err:
            return ToolResult(
                content="Could not resolve the document reference to a file.",
                is_error=True,
                error_kind=ToolErrorKind.NOT_FOUND,
            )
        file = files_or_err[0]
        try:
            await service.upload_compliance_document(
                customer_id=customer_id,
                compliance_type=compliance_type,
                file=file,
            )
        except Exception as exc:
            return service_error_to_tool_result("uploading compliance document", exc)

        return ToolResult(
            content=(f"Uploaded {compliance_type} document for customer {customer_id}."),
            receipt=ToolReceipt(
                action="Uploaded AppFolio compliance document",
                target=f"customer {customer_id} ({compliance_type})",
            ),
        )

    return [
        Tool(
            name=ToolName.APPFOLIO_UPLOAD_COMPLIANCE_DOC,
            description=(
                "Upload a compliance document (W-9, COI, license) for a"
                " specific AppFolio property manager."
            ),
            function=appfolio_upload_compliance_doc,
            params_model=AppFolioUploadComplianceDocParams,
            usage_hint=(
                "Confirm the compliance type (e.g. 'w9', 'general_liability')"
                " with the user before uploading. AppFolio scopes the doc to"
                " one customer at a time."
            ),
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ASK,
                description_builder=lambda args: (
                    f"Upload {args.get('compliance_type', '?')} document"
                    f" for AppFolio customer {args.get('customer_id', '?')}"
                ),
            ),
        ),
    ]
