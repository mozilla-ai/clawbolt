def make_telegram_update_payload(
    chat_id: int = 123456789,
    text: str = "Hello Backshop",
    message_id: int = 42,
    update_id: int = 100,
    photo_file_id: str | None = None,
    voice_file_id: str | None = None,
    voice_mime_type: str = "audio/ogg",
    document_file_id: str | None = None,
    document_mime_type: str = "application/pdf",
    first_name: str = "Test",
) -> dict:
    """Build a realistic Telegram webhook Update JSON payload."""
    msg: dict = {
        "message_id": message_id,
        "from": {
            "id": chat_id,
            "is_bot": False,
            "first_name": first_name,
        },
        "chat": {
            "id": chat_id,
            "first_name": first_name,
            "type": "private",
        },
        "date": 1700000000,
        "text": text,
    }

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
        # When photo is present, text field may be empty
        if text == "Hello Backshop":
            msg["text"] = ""

    if voice_file_id:
        msg["voice"] = {
            "file_id": voice_file_id,
            "file_unique_id": "voice1",
            "duration": 5,
            "mime_type": voice_mime_type,
        }
        if text == "Hello Backshop":
            msg["text"] = ""

    if document_file_id:
        msg["document"] = {
            "file_id": document_file_id,
            "file_unique_id": "doc1",
            "file_name": "estimate.pdf",
            "mime_type": document_mime_type,
        }
        if text == "Hello Backshop":
            msg["text"] = ""

    return {
        "update_id": update_id,
        "message": msg,
    }
