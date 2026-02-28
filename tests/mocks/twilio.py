def make_twilio_webhook_payload(
    from_number: str = "+15551234567",
    body: str = "Hello Backshop",
    num_media: int = 0,
    media_urls: list[str] | None = None,
    media_types: list[str] | None = None,
    message_sid: str = "SM1234567890abcdef",
    account_sid: str = "AC1234567890abcdef",
    to_number: str = "+15559876543",
) -> dict[str, str]:
    """Build a realistic Twilio webhook form payload."""
    payload: dict[str, str] = {
        "From": from_number,
        "To": to_number,
        "Body": body,
        "NumMedia": str(num_media),
        "MessageSid": message_sid,
        "AccountSid": account_sid,
    }

    if media_urls:
        for i, url in enumerate(media_urls):
            payload[f"MediaUrl{i}"] = url
    if media_types:
        for i, mime in enumerate(media_types):
            payload[f"MediaContentType{i}"] = mime

    return payload
