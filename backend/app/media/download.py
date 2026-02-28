import datetime
from dataclasses import dataclass

import httpx

from backend.app.config import settings

MIME_EXTENSIONS: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "audio/ogg": ".ogg",
    "audio/mp3": ".mp3",
    "audio/mpeg": ".mp3",
    "audio/amr": ".amr",
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


async def download_twilio_media(
    url: str,
    twilio_auth: tuple[str, str] | None = None,
) -> DownloadedMedia:
    """Download media from a Twilio URL with authentication."""
    auth = twilio_auth or (settings.twilio_account_sid, settings.twilio_auth_token)

    async with httpx.AsyncClient() as client:
        response = await client.get(url, auth=auth, follow_redirects=True, timeout=30.0)
        response.raise_for_status()

    mime_type = response.headers.get("content-type", "application/octet-stream").split(";")[0]
    filename = _generate_filename(mime_type)

    return DownloadedMedia(
        content=response.content,
        mime_type=mime_type,
        original_url=url,
        filename=filename,
    )
