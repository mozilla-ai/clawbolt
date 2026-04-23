import asyncio
import datetime
import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx

from backend.app.config import TELEGRAM_API_BASE, settings


def _parse_content_length(raw: str) -> int | None:
    """Parse a Content-Length header value, returning None if absent or invalid.

    Defensive against whitespace, signs, and chunked encoding placeholders so
    a malformed header never silently bypasses the size guard.
    """
    if not raw:
        return None
    try:
        value = int(raw.strip())
    except ValueError:
        return None
    return value if value >= 0 else None


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


async def download_bounded(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_bytes: int | None = None,
    max_seconds: float | None = None,
    follow_redirects: bool = True,
    params: dict[str, Any] | httpx.QueryParams | None = None,
) -> tuple[bytes, httpx.Headers]:
    """Stream-download *url* with hard caps on body size and total wall time.

    Aborts mid-stream the moment the running byte count exceeds ``max_bytes``,
    so an oversized payload never fully buffers and never OOMs the worker.
    Wraps the whole download in ``asyncio.wait_for`` so a slow-drip server
    that trickles bytes forever can't hold a worker indefinitely.

    Defaults pull from ``settings.max_media_size_bytes`` and
    ``settings.media_download_max_seconds`` respectively.

    Returns ``(content, response_headers)``. Raises ``ValueError`` for size
    violations, ``TimeoutError`` for the wall-time deadline (asyncio re-raises
    its own ``TimeoutError`` as the builtin on 3.11+), and ``httpx.HTTPError``
    for transport failures.
    """
    limit = max_bytes if max_bytes is not None else settings.max_media_size_bytes
    deadline = max_seconds if max_seconds is not None else settings.media_download_max_seconds

    async def _do_stream() -> tuple[bytes, httpx.Headers]:
        async with client.stream(
            "GET", url, params=params, follow_redirects=follow_redirects
        ) as resp:
            resp.raise_for_status()
            # Reject upfront when the server volunteered a too-big size.
            # Parse defensively: trim whitespace, accept only non-negative ints.
            declared = _parse_content_length(resp.headers.get("content-length", ""))
            if declared is not None and declared > limit:
                raise ValueError(
                    f"Media file too large: declared {declared} bytes (limit {limit} bytes)"
                )
            chunks: list[bytes] = []
            total = 0
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > limit:
                    raise ValueError(
                        f"Media file too large: exceeded {limit} bytes during streaming"
                    )
                chunks.append(chunk)
            return b"".join(chunks), resp.headers

    return await asyncio.wait_for(_do_stream(), timeout=deadline)


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

        # Step 2: download the file with hard size + wall-time bounds
        file_url = f"{TELEGRAM_API_BASE}/file/bot{token}/{file_path}"
        content, headers = await download_bounded(client, file_url)

    mime_type = headers.get("content-type", DEFAULT_MIME_TYPE).split(";")[0]

    # Telegram's file download endpoint often returns application/octet-stream
    # regardless of the actual file type.  Infer from the file path extension.
    if mime_type == DEFAULT_MIME_TYPE:
        ext = os.path.splitext(file_path)[1].lower()
        inferred = _EXTENSION_TO_MIME.get(ext)
        if inferred:
            logger.debug("Inferred MIME type %s from file path extension %s", inferred, ext)
            mime_type = inferred

    logger.info(
        "Download complete: file_id=%s, mime_type=%s, size=%d bytes",
        file_id,
        mime_type,
        len(content),
    )
    filename = generate_filename(mime_type)

    return DownloadedMedia(
        content=content,
        mime_type=mime_type,
        original_url=file_id,
        filename=filename,
    )
