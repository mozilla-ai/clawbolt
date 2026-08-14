"""Best-effort PII redaction for admin-surfaced user content.

Used by ``/admin/shared-data/*`` endpoints (issue #325 work item 3) to
redact obvious PII from message bodies before they reach an admin's
screen. Pattern set mirrors octonous's redactor: emails, phone numbers
(international and US), credit-card-like sequences, and high-entropy
token-shaped runs.

Limits:

- Regex-based, so this catches the obvious shapes (``+15555550123``,
  ``user@example.com``) but misses anything that doesn't match a
  pattern (e.g. a person's name buried in a sentence). Treat this as
  a defense-in-depth layer, NOT a sufficient guarantee that the
  response carries no PII. The consent gate is the primary guarantee;
  this just narrows the blast radius if a consenting user turns out
  to have shared something sensitive.
- The token pattern (20+ alphanumeric chars) catches real API keys
  and JWTs but also anything that happens to be 20+ chars of
  ``[A-Za-z0-9_-]`` (e.g. base64-ish UUID slugs). Acceptable; the
  redaction is conservative-by-default.
- Order matters: emails are checked before phones because a phone-
  shaped local part of an email (``+15555550123@example.com``) should
  redact as a whole email, not as a phone followed by a domain.
"""

from __future__ import annotations

import re
from typing import Any

_EMAIL = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_CREDIT_CARD = re.compile(r"\d{4}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}")
# International phone with leading '+' followed by digits / separators.
_PHONE_INTL = re.compile(r"\+\d[\d\-\s.]{7,}\d")
# US-style phones with parentheses around the area code.
_PHONE_US = re.compile(r"\(\d{3}\)\s*\d{3}[\s\-.]?\d{4}")
# Any long alphanumeric run looks like an API key / JWT / token slug.
_TOKEN = re.compile(r"[A-Za-z0-9_\-]{20,}")


def redact_pii(text: str) -> str:
    """Return *text* with email / phone / card / token shapes replaced.

    Matches are replaced with bracketed labels so the surrounding
    sentence stays readable: ``"Call me at [PHONE]"`` instead of just
    silent removal. This is intentional — admins reading shared
    conversations need to know that PII *was* there even when the
    value itself is masked.
    """
    if not text:
        return text
    result = _EMAIL.sub("[EMAIL]", text)
    result = _CREDIT_CARD.sub("[CARD]", result)
    result = _PHONE_INTL.sub("[PHONE]", result)
    result = _PHONE_US.sub("[PHONE]", result)
    result = _TOKEN.sub("[TOKEN]", result)
    return result


def redact_pii_recursive(value: Any) -> Any:
    """Recursively redact PII inside nested dict / list / scalar structures.

    Walks the structure and applies :func:`redact_pii` to every string
    leaf. Other scalars (int, float, bool, None) pass through unchanged
    because the regex set is shape-based and would never match a number.

    Used by ``/admin/shared-data/*/turns`` to redact tool-call ``args``
    (which the LLM populates with arbitrary nested JSON) and tool-call
    ``result`` payloads, so a query like ``qb_query("... WHERE
    customer_name = 'John Smith'")`` does not surface the customer name
    even when an admin pulls the conversation.
    """
    if isinstance(value, str):
        return redact_pii(value)
    if isinstance(value, dict):
        return {k: redact_pii_recursive(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_pii_recursive(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_pii_recursive(item) for item in value)
    return value
