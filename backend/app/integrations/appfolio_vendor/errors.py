"""Shared error-to-ToolResult translation and diagnostic logging.

Every tool that calls into :class:`AppFolioVendorService` ends up
funnelling the same exception types into ToolResult shapes. Centralize
that mapping here so the auth-expired hint, the service-error wrapping,
and the unexpected-error logging stay identical across tool modules.

This module also exposes :func:`log_unexpected_response_shape`, used
by the read-side parsers. AppFolio's API has a long history of small
shape surprises (camelCase vs snake_case, nested vs flat, optional
envelope wrappers); when our parser walks a 200-OK response and finds
nothing usable, the user-facing path returns a generic "no results"
message and we have nothing to debug from after the fact. The helper
makes that case loud in the production logs so the *first* report of
a new shape is enough to understand and fix it.
"""

from __future__ import annotations

import logging
from typing import Any

from backend.app.agent.tools.base import ToolErrorKind, ToolResult
from backend.app.integrations.appfolio_vendor.service import (
    AppFolioError,
    AuthExpiredError,
    AuthScopeError,
)

logger = logging.getLogger(__name__)


# Cap on how much of the actual response we paste into the log line.
# Wide enough to capture the structure (a few keys plus their values)
# without burying it in base64-encoded photo payloads or massive lists.
_DIAG_REPR_LIMIT = 1500


def log_unexpected_response_shape(
    tool_label: str,
    payload: Any,
    *,
    expected: str,
) -> None:
    """Emit a structured WARNING describing a successful response we couldn't parse.

    Call this from any read-side AppFolio parser when AppFolio answered
    HTTP 200 (so the request itself is fine) but we couldn't find the
    fields we expected in the body. The log line names the parser, what
    it was looking for, and what the response actually looks like:

    * For dicts, the top-level keys (sorted, so diffs across runs are
      stable).
    * For lists, length and the first item's keys when it is a dict.
    * For anything else, just the type name.
    * Plus a truncated ``repr()`` of the payload itself so the structure
      is visible without re-running the request.

    Use ``tool_label`` to name the parser ("appfolio_get_work_order
    address extraction"), and ``expected`` to describe what the parser
    was looking for ("address fields at top level or nested under
    `address`/`location`").
    """
    if isinstance(payload, dict):
        shape = f"dict keys={sorted(payload.keys())!r}"
    elif isinstance(payload, list):
        if payload and isinstance(payload[0], dict):
            sample = sorted(payload[0].keys())
            shape = f"list len={len(payload)} sample_keys={sample!r}"
        else:
            shape = f"list len={len(payload)}"
    else:
        shape = f"type={type(payload).__name__}"

    body_preview = repr(payload)
    if len(body_preview) > _DIAG_REPR_LIMIT:
        body_preview = body_preview[:_DIAG_REPR_LIMIT] + f"...<{len(body_preview)} chars>"

    logger.warning(
        "AppFolio %s: response did not match expected shape (%s) | actual=%s | body=%s",
        tool_label,
        expected,
        shape,
        body_preview,
    )


_AUTH_EXPIRED_HINT = (
    "Have the user reconnect AppFolio in the Clawbolt web app under Settings"
    " with a fresh magic link. Do not ask them to paste the link into chat."
)

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
