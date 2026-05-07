"""Shared error-to-ToolResult translation for AppFolio tools.

Every tool that calls into :class:`AppFolioVendorService` ends up
funnelling the same exception types into ToolResult shapes. Centralize
that mapping here so the auth-expired hint, the service-error wrapping,
and the unexpected-error logging stay identical across tool modules.
"""

from __future__ import annotations

import logging

from backend.app.agent.tools.base import ToolErrorKind, ToolResult
from backend.app.integrations.appfolio_vendor.service import (
    AppFolioError,
    AuthExpiredError,
)

logger = logging.getLogger(__name__)


_AUTH_EXPIRED_HINT = "Have the user request a fresh magic link and re-run appfolio_connect."


def service_error_to_tool_result(method_label: str, exc: BaseException) -> ToolResult:
    """Map an AppFolio service exception to a populated :class:`ToolResult`.

    ``method_label`` is a short verb phrase ("scheduling work order",
    "creating invoice") woven into the user-facing message. The function
    handles the three exception classes the service layer can raise plus
    the catch-all path; for the catch-all it logs an exception trace
    so the failure is visible even when the agent only surfaces the
    short ToolResult message to the user.
    """
    if isinstance(exc, AuthExpiredError):
        return ToolResult(
            content=f"AppFolio session expired while {method_label}.",
            is_error=True,
            error_kind=ToolErrorKind.AUTH,
            hint=_AUTH_EXPIRED_HINT,
        )
    if isinstance(exc, AppFolioError):
        return ToolResult(
            content=f"AppFolio error while {method_label}: {exc}",
            is_error=True,
            error_kind=ToolErrorKind.SERVICE,
        )
    logger.exception("Unexpected AppFolio failure %s", method_label)
    return ToolResult(
        content=f"Unexpected error while {method_label}: {exc}",
        is_error=True,
        error_kind=ToolErrorKind.INTERNAL,
    )
