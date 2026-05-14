"""Shared helpers for Google REST API error responses.

Gmail and Google Calendar return the same JSON error envelope:

    {"error": {"code": ..., "message": "...", "errors": [{"reason": "..."}]}}

Both integrations need the same logic to extract the human-readable message
and the machine-readable reason so callers can render the actual cause to
the user instead of a hardcoded guess. Lives at the ``integrations/`` level
(not inside one integration) so neither integration owns the other's helper.
"""

from __future__ import annotations

import json

# Cap the message text we surface so a verbose 403 (which can include a
# console URL and several sentences) doesn't blow past a reasonable agent
# reply. 500 fits Gmail's real accessNotConfigured message in full, including
# the "wait a few minutes for propagation" sentence that matters right after
# the operator enables the API.
GOOGLE_MAX_MESSAGE_CHARS = 500


def parse_google_api_error(body: str) -> tuple[str, str]:
    """Return ``(message, reason)`` from a Google REST API JSON error body.

    Returns empty strings when the body is missing, not JSON, or lacks the
    expected shape so callers can fall through to a generic message.
    """
    if not body:
        return "", ""
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return "", ""
    err = data.get("error") if isinstance(data, dict) else None
    if not isinstance(err, dict):
        return "", ""
    message = err.get("message") or ""
    reason = ""
    errors = err.get("errors")
    if isinstance(errors, list) and errors and isinstance(errors[0], dict):
        reason = errors[0].get("reason") or ""
    return message, reason


def format_google_api_message(message: str, max_chars: int = GOOGLE_MAX_MESSAGE_CHARS) -> str:
    """Truncate a Google error message for inclusion in a ToolResult."""
    if len(message) <= max_chars:
        return message
    return message[:max_chars].rstrip() + "..."


__all__ = [
    "GOOGLE_MAX_MESSAGE_CHARS",
    "format_google_api_message",
    "parse_google_api_error",
]
