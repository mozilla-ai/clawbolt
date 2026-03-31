import datetime
import logging
import os
from dataclasses import dataclass

import httpx

from backend.app.config import TELEGRAM_API_BASE, settings

logger = logging.getLogger(__name__)

DEFAULT_MIME_TYPE = "application/octet-stream"

MIME_EXTENSIONS: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
}

# Reverse lookup: extension -> MIME type (e.g. ".jpg" -> "image/jpeg")
_EXTENSION_TO_MIME: dict[str, str] = {ext: mime for mime, ext in MIME_EXTENSIONS.items()}


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
    if mime_type == "application/pdf":
        return "pdf"
    return "unknown"


def generate_filename(mime_type: str) -> str:
    """Generate a filename from MIME type and timestamp."""
    ext = MIME_EXTENSIONS.get(mime_type, ".bin")
    timestamp = datetime.datetime.now(tz=datetime.UTC).strftime("%Y%m%d_%H%M%S")
    return f"media_{timestamp}{ext}"


def check_media_size(content: bytes) -> int:
    """Raise ``ValueError`` if *content* exceeds the configured media size limit.

    Returns the size in bytes so callers can reuse it without a second ``len()`` call.
    """
    size_bytes = len(content)
    if size_bytes > settings.max_media_size_bytes:
        msg = (
            f"Media file too large: {size_bytes} bytes "
            f"(limit {settings.max_media_size_bytes} bytes)"
        )
        raise ValueError(msg)
    return size_bytes


async def download_telegram_media(
    file_id: str,
    bot_token: str | None = None,
) -> DownloadedMedia:
    """Download media from Telegram via the Bot API.

    Flow: file_id -> GET /bot{token}/getFile -> file_path -> download bytes.
    """
    token = bot_token or settings.telegram_bot_token
    api_base = f"{TELEGRAM_API_BASE}/bot{token}"

    logger.info("Downloading Telegram media: file_id=%s", file_id)

    async with httpx.AsyncClient() as client:
        # Step 1: get file path
        resp = await client.get(
            f"{api_base}/getFile",
            params={"file_id": file_id},
            timeout=settings.http_timeout_seconds,
        )
        resp.raise_for_status()
        file_path = resp.json()["result"]["file_path"]

        # Step 2: download the file
        file_url = f"{TELEGRAM_API_BASE}/file/bot{token}/{file_path}"
        download = await client.get(
            file_url, follow_redirects=True, timeout=settings.http_timeout_seconds
        )
        download.raise_for_status()

    mime_type = download.headers.get("content-type", DEFAULT_MIME_TYPE).split(";")[0]

    # Telegram's file download endpoint often returns application/octet-stream
    # regardless of the actual file type.  Infer from the file path extension.
    if mime_type == DEFAULT_MIME_TYPE:
        ext = os.path.splitext(file_path)[1].lower()
        inferred = _EXTENSION_TO_MIME.get(ext)
        if inferred:
            logger.debug("Inferred MIME type %s from file path extension %s", inferred, ext)
            mime_type = inferred

    size_bytes = check_media_size(download.content)

    logger.info(
        "Download complete: file_id=%s, mime_type=%s, size=%d bytes",
        file_id,
        mime_type,
        size_bytes,
    )
    filename = generate_filename(mime_type)

    return DownloadedMedia(
        content=download.content,
        mime_type=mime_type,
        original_url=file_id,
        filename=filename,
    )
