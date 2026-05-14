"""HTTP error classification for CompanyCam tools.

CompanyCam tools wrap every service call in a broad ``except Exception`` so
the agent gets a structured ToolResult instead of a raw traceback. The
default classification (``SERVICE``) is the right hint for connection
errors and 5xx, but misleads the agent on 401/403/404 by suggesting
"try a different approach" when the real fix is "reconnect", "this
operation is not permitted", or "the resource doesn't exist".

This module deliberately does not parse CompanyCam's response body. The
caller already renders ``f"CompanyCam error: {exc}"`` which surfaces the
real exception string verbatim; the only thing we need from the status
code is the right ``ToolErrorKind`` so the LLM hint matches the cause.
"""

from __future__ import annotations

import httpx

from backend.app.agent.tools.base import ToolErrorKind


def classify_companycam_error(exc: BaseException) -> ToolErrorKind:
    """Map a raised exception to the right ``ToolErrorKind``.

    Non-HTTP exceptions and 5xx responses fall through to ``SERVICE``.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 401:
            return ToolErrorKind.AUTH
        if status == 403:
            return ToolErrorKind.PERMISSION
        if status == 404:
            return ToolErrorKind.NOT_FOUND
    return ToolErrorKind.SERVICE


__all__ = ["classify_companycam_error"]
