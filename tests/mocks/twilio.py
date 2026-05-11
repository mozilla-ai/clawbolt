"""Helpers for crafting Twilio webhook fixtures in tests."""

from twilio.request_validator import RequestValidator

TWILIO_TEST_AUTH_TOKEN = "test-twilio-auth-token-12345"


def make_twilio_form(
    sender: str = "+15551234567",
    to: str = "+15559876543",
    text: str = "Hello Clawbolt",
    message_sid: str = "SM" + "0" * 30 + "01",
    media: list[tuple[str, str]] | None = None,
) -> dict[str, str]:
    """Build a Twilio inbound webhook form payload matching the real shape.

    ``media`` is a list of ``(url, content_type)`` tuples that becomes
    ``NumMedia`` + ``MediaUrl{N}`` + ``MediaContentType{N}`` fields.
    """
    form: dict[str, str] = {
        "From": sender,
        "To": to,
        "Body": text,
        "MessageSid": message_sid,
        "SmsMessageSid": message_sid,
        "AccountSid": "AC" + "0" * 32,
        "NumMedia": str(len(media) if media else 0),
    }
    if media:
        for i, (url, content_type) in enumerate(media):
            form[f"MediaUrl{i}"] = url
            form[f"MediaContentType{i}"] = content_type
    return form


def sign_twilio_form(
    url: str,
    form: dict[str, str],
    auth_token: str = TWILIO_TEST_AUTH_TOKEN,
) -> str:
    """Compute the ``X-Twilio-Signature`` for *form* at *url*.

    Mirrors what Twilio's edge does so test webhook POSTs can pass the
    signature validation gate when it's enabled.
    """
    validator = RequestValidator(auth_token)
    return validator.compute_signature(url, form)
