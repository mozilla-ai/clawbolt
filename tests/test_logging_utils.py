"""Tests for PII masking in log output."""

import pytest

from backend.app.logging_utils import mask_pii


@pytest.mark.parametrize(
    "value, expected",
    [
        # Phone numbers (E.164 and bare digits)
        ("+14025551234", "+***1234"),
        ("+12034007249", "+***7249"),
        ("14025551234", "***1234"),
        ("+442071234567", "+***4567"),
        # Email addresses
        ("luke@companycam.com", "l***@companycam.com"),
        ("a@b.com", "a***@b.com"),
        ("test.user@example.org", "t***@example.org"),
        # Compound identifiers (iMessage chat GUIDs)
        ("iMessage;-;+14025551234", "iMessage;-;+***1234"),
        ("SMS;-;+14025551234", "SMS;-;+***1234"),
        # Non-PII passthrough
        ("some-uuid-string", "some-uuid-string"),
        ("8aff2847-84d6-4fcd-8a9b-1c2ce16a54a5", "8aff2847-84d6-4fcd-8a9b-1c2ce16a54a5"),
        ("telegram", "telegram"),
        ("", ""),
        # Too short for phone
        ("12345", "12345"),
        ("123456", "123456"),
    ],
)
def test_mask_pii(value: str, expected: str) -> None:
    assert mask_pii(value) == expected
