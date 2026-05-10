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
    AuthScopeError,
)

logger = logging.getLogger(__name__)


_AUTH_EXPIRED_HINT = "Have the user request a fresh magic link and re-run appfolio_connect."

# Distinct hint for scope failures: reconnecting does not help, the
# request just used the wrong customer_id. Steers the agent toward
# resolving the canonical customer (via list_work_orders or the
# service's ``_resolve_primary_customer_id`` helper) rather than asking
# the user for a fresh magic link they do not need.
_AUTH_SCOPE_HINT = (
    "Do not ask the user to reconnect. The AppFolio session is valid;"
    " the request just used a customer_id the JWT is not authorized for."
    " Resolve the right customer_id (e.g. via appfolio_list_work_orders)"
    " and retry."
)


def service_error_to_tool_result(method_label: str, exc: BaseException) -> ToolResult:
    """Map an AppFolio service exception to a populated :class:`ToolResult`.

    ``method_label`` is a short verb phrase ("scheduling work order",
    "creating invoice") woven into the user-facing message. The function
    handles the auth, generic, and unexpected paths; for the catch-all
    it logs an exception trace so the failure is visible even when the
    agent only surfaces the short ToolResult message to the user.

    The two auth flavours are kept separate. ``AuthExpiredError`` means
    the magic-link credential genuinely expired and the user must
    reconnect. ``AuthScopeError`` means the credential is fine but the
    request used the wrong customer_id; reconnecting will not help and
    telling the user to reconnect erodes their trust when the next
    reconnect produces the same 401.
    """
    if isinstance(exc, AuthExpiredError):
        return ToolResult(
            content=f"AppFolio session expired while {method_label}.",
            is_error=True,
            error_kind=ToolErrorKind.AUTH,
            hint=_AUTH_EXPIRED_HINT,
        )
    if isinstance(exc, AuthScopeError):
        return ToolResult(
            content=(
                f"AppFolio rejected the request while {method_label}:"
                " the customer_id is not authorized for this session."
            ),
            is_error=True,
            error_kind=ToolErrorKind.SERVICE,
            hint=_AUTH_SCOPE_HINT,
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
