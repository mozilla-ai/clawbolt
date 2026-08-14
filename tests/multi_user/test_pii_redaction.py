"""Tests for the PII redaction helper used by /admin/shared-data."""

from __future__ import annotations

from backend.app.services.pii_redaction import redact_pii, redact_pii_recursive


def test_empty_input_passthrough() -> None:
    assert redact_pii("") == ""


def test_email_is_replaced() -> None:
    assert redact_pii("Reach me at jane.doe@example.com please") == ("Reach me at [EMAIL] please")


def test_international_phone_is_replaced() -> None:
    assert redact_pii("Call +15555550123 anytime") == "Call [PHONE] anytime"


def test_us_style_phone_is_replaced() -> None:
    assert redact_pii("Call (555) 555-1234 today") == "Call [PHONE] today"


def test_credit_card_is_replaced() -> None:
    """Sixteen-digit grouped numbers (with or without separators) are masked."""
    assert redact_pii("Card 4111 1111 1111 1111") == "Card [CARD]"
    assert redact_pii("Card 4111-1111-1111-1111") == "Card [CARD]"
    assert redact_pii("Card 4111111111111111") == "Card [CARD]"


def test_token_shape_is_replaced() -> None:
    """Long alphanumeric runs (20+ chars) are masked.

    Aimed at API keys / JWTs / session ids — anything an admin
    incidentally reading a transcript shouldn't have to see in plaintext.
    The pattern is conservative; it'll also catch unrelated 20+ char
    slugs (e.g. base64 UUIDs), which we accept as a redaction tradeoff.
    """
    raw = "Use API key sk_test_AbCdEfGhIjKlMnOpQrSt to authenticate"
    assert redact_pii(raw) == "Use API key [TOKEN] to authenticate"


def test_multiple_pii_in_one_string() -> None:
    raw = "Email jane@example.com or call +15555550123 about card 4111 1111 1111 1111"
    out = redact_pii(raw)
    assert "jane@example.com" not in out
    assert "+15555550123" not in out
    assert "4111 1111 1111 1111" not in out
    assert "[EMAIL]" in out
    assert "[PHONE]" in out
    assert "[CARD]" in out


def test_short_string_with_no_pii_unchanged() -> None:
    """Short prose without obvious PII shapes passes through untouched."""
    assert redact_pii("hello") == "hello"
    assert redact_pii("see you tomorrow") == "see you tomorrow"


def test_phone_aliased_email_redacts_as_email_first() -> None:
    """Order matters: ``+15555550123@example.com`` should redact as a
    single ``[EMAIL]`` token, not as ``[PHONE]@example.com``.

    We run the email regex before the phone regex precisely so
    phone-shaped local parts of an email don't fragment the redaction.
    """
    assert redact_pii("Send to +15555550123@example.com") == "Send to [EMAIL]"


def test_recursive_redacts_nested_dict_values() -> None:
    """``redact_pii_recursive`` walks dict values and applies redact_pii to strings."""
    raw = {"customer": "Email me at jane@example.com", "id": 42}
    out = redact_pii_recursive(raw)
    assert out == {"customer": "Email me at [EMAIL]", "id": 42}


def test_recursive_redacts_nested_lists_and_mixed_types() -> None:
    """Lists, nested dicts, and non-string scalars all walked correctly.

    Numbers and booleans pass through untouched because the regex set
    is shape-based and would never match a numeric value.
    """
    raw = {
        "filters": [{"field": "phone", "value": "+15555550123"}, {"field": "amount", "value": 100}],
        "active": True,
        "notes": None,
    }
    out = redact_pii_recursive(raw)
    assert out == {
        "filters": [{"field": "phone", "value": "[PHONE]"}, {"field": "amount", "value": 100}],
        "active": True,
        "notes": None,
    }


def test_recursive_passthrough_for_scalars() -> None:
    """Bare ints / floats / bools / None go through unchanged."""
    assert redact_pii_recursive(42) == 42
    assert redact_pii_recursive(1.5) == 1.5
    assert redact_pii_recursive(True) is True
    assert redact_pii_recursive(None) is None


def test_recursive_redacts_top_level_string() -> None:
    """A plain string at the top level still gets redacted (no wrapper required)."""
    assert redact_pii_recursive("ping +15555550123 now") == "ping [PHONE] now"
