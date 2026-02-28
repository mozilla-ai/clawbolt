import datetime
from dataclasses import dataclass

import httpx

from backend.app.config import settings

MIME_EXTENSIONS: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "audio/ogg": ".ogg",
    "audio/mp3": ".mp3",
    "audio/mpeg": ".mp3",
    "audio/amr": ".amr",
    "audio/wav": ".wav",
    "video/mp4": ".mp4",
    "video/3gpp": ".3gp",
    "application/pdf": ".pdf",
}


@dataclass
class DownloadedMedia:
    content: bytes
    mime_type: str
    original_url: str
    filename: str


def classify_media(mime_type: str) -> str:
    """Classify MIME type into processing category."""
    if mime_type.startswith("image/"):
        return "image"
    if mime_type.startswith("audio/"):
        return "audio"
    if mime_type.startswith("video/"):
        return "video"
    if mime_type == "application/pdf":
        return "pdf"
    return "unknown"


def _generate_filename(mime_type: str) -> str:
    """Generate a filename from MIME type and timestamp."""
    ext = MIME_EXTENSIONS.get(mime_type, ".bin")
    timestamp = datetime.datetime.now(tz=datetime.UTC).strftime("%Y%m%d_%H%M%S")
    return f"media_{timestamp}{ext}"


async def download_telegram_media(
    file_id: str,
    bot_token: str | None = None,
) -> DownloadedMedia:
    """Download media from Telegram via the Bot API.

    Flow: file_id -> GET /bot{token}/getFile -> file_path -> download bytes.
    """
    token = bot_token or settings.telegram_bot_token
    api_base = f"https://api.telegram.org/bot{token}"

    async with httpx.AsyncClient() as client:
        # Step 1: get file path
        resp = await client.get(f"{api_base}/getFile", params={"file_id": file_id}, timeout=30.0)
        resp.raise_for_status()
        file_path = resp.json()["result"]["file_path"]

        # Step 2: download the file
        file_url = f"https://api.telegram.org/file/bot{token}/{file_path}"
        download = await client.get(file_url, follow_redirects=True, timeout=30.0)
        download.raise_for_status()

    mime_type = download.headers.get("content-type", "application/octet-stream").split(";")[0]
    filename = _generate_filename(mime_type)

    return DownloadedMedia(
        content=download.content,
        mime_type=mime_type,
        original_url=file_id,
        filename=filename,
    )
