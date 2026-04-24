"""Logging helpers for PII redaction.

All user-identifying data (phone numbers, email addresses) MUST be masked
before being written to logs.  Import ``mask_pii`` and wrap any value that
might contain a phone number or email before interpolating it into a log
message.
"""

from __future__ import annotations

import re

# Email: keep first char of local part + full domain.
_EMAIL_RE = re.compile(r"^([^@])[^@]*(@.+)$")

# Embedded phone inside a compound identifier like "iMessage;-;+14025551234".
_EMBEDDED_PHONE_RE = re.compile(r"\+\d{7,}")


def _mask_phone(value: str) -> str:
    """Mask a bare phone number string, keeping only the last 4 digits."""
    prefix = "+" if value.startswith("+") else ""
    digits = value.lstrip("+")
    if len(digits) <= 4:
        return value
    return f"{prefix}***{digits[-4:]}"


def mask_pii(value: str) -> str:
    """Mask phone numbers and email addresses for safe logging.

    Handles bare phone numbers, emails, and compound identifiers that
    embed a phone number (e.g. iMessage chat GUIDs).
    """
    if not value:
        return value

    # Email (contains @)
    if "@" in value:
        m = _EMAIL_RE.match(value)
        if m:
            return f"{m.group(1)}***{m.group(2)}"
        return value

    # Bare phone number (starts with + or all digits, 7+ digits)
    stripped = value.lstrip("+")
    if stripped.isdigit() and len(stripped) >= 7:
        return _mask_phone(value)

    # Compound identifier with embedded phone (e.g. "iMessage;-;+1...")
    if "+" in value:
        result = _EMBEDDED_PHONE_RE.sub(lambda m: _mask_phone(m.group(0)), value)
        if result != value:
            return result

    return value
