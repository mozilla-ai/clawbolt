def make_telegram_update_payload(
    chat_id: int = 123456789,
    text: str = "Hello Clawbolt",
    message_id: int = 42,
    update_id: int = 100,
    photo_file_id: str | None = None,
    document_file_id: str | None = None,
    document_mime_type: str = "application/pdf",
    video_file_id: str | None = None,
    video_mime_type: str = "video/mp4",
    video_note_file_id: str | None = None,
    caption: str | None = None,
    first_name: str = "Test",
    username: str | None = None,
) -> dict:
    """Build a realistic Telegram webhook Update JSON payload.

    For media messages, Telegram puts user text in ``caption`` rather than
    ``text``.  Pass ``caption="..."`` to simulate this.  When any media
    field is set and the caller did not provide explicit ``text``, the
    ``text`` key is omitted from the payload (matching real Telegram
    behaviour).  If ``caption`` is provided it is set on the message
    instead.
    """
    from_obj: dict = {
        "id": chat_id,
        "is_bot": False,
        "first_name": first_name,
    }
    if username is not None:
        from_obj["username"] = username

    has_media = any(
        [
            photo_file_id,
            document_file_id,
            video_file_id,
            video_note_file_id,
        ]
    )

    msg: dict = {
        "message_id": message_id,
        "from": from_obj,
        "chat": {
            "id": chat_id,
            "first_name": first_name,
            "type": "private",
        },
        "date": 1700000000,
    }

    # Telegram uses "text" for plain messages and "caption" for media.
    # When media is present and text was not explicitly provided, omit
    # the text key to match real payloads.
    if has_media and text == "Hello Clawbolt":
        # Default text with media: omit text, use caption if provided
        if caption is not None:
            msg["caption"] = caption
    elif has_media and text:
        # Caller explicitly set text alongside media: treat as caption
        msg["caption"] = text
    else:
        msg["text"] = text

    if caption is not None and "caption" not in msg:
        msg["caption"] = caption

    if photo_file_id:
        msg["photo"] = [
            {
                "file_id": photo_file_id,
                "file_unique_id": "abc",
                "width": 90,
                "height": 90,
                "file_size": 1000,
            },
            {
                "file_id": photo_file_id,
                "file_unique_id": "def",
                "width": 320,
                "height": 320,
                "file_size": 5000,
            },
        ]

    if video_file_id:
        msg["video"] = {
            "file_id": video_file_id,
            "file_unique_id": "vid1",
            "duration": 10,
            "width": 1280,
            "height": 720,
            "mime_type": video_mime_type,
        }

    if video_note_file_id:
        msg["video_note"] = {
            "file_id": video_note_file_id,
            "file_unique_id": "vnote1",
            "duration": 5,
            "length": 240,
        }

    if document_file_id:
        msg["document"] = {
            "file_id": document_file_id,
            "file_unique_id": "doc1",
            "file_name": "estimate.pdf",
            "mime_type": document_mime_type,
        }

    return {
        "update_id": update_id,
        "message": msg,
    }
