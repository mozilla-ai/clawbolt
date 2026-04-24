import asyncio
from collections.abc import AsyncIterator
from contextlib import AbstractContextManager
from unittest.mock import patch

import httpx
import pytest

from backend.app.media.download import (
    DownloadedMedia,
    _parse_content_length,
    classify_media,
    download_bounded,
    download_telegram_media,
)


def test_classify_image_types() -> None:
    assert classify_media("image/jpeg") == "image"
    assert classify_media("image/png") == "image"
    assert classify_media("image/gif") == "image"


def test_classify_audio_treated_as_unknown() -> None:
    assert classify_media("audio/ogg") == "unknown"
    assert classify_media("audio/mp3") == "unknown"
    assert classify_media("audio/wav") == "unknown"


def test_classify_video_treated_as_unknown() -> None:
    assert classify_media("video/mp4") == "unknown"
    assert classify_media("video/quicktime") == "unknown"


def test_classify_pdf() -> None:
    assert classify_media("application/pdf") == "pdf"


def test_classify_unknown() -> None:
    assert classify_media("application/zip") == "unknown"
    assert classify_media("text/plain") == "unknown"


def _telegram_transport(
    *,
    file_path: str = "photos/file_0.jpg",
    download_body: bytes = b"fake-image-bytes",
    download_content_type: str = "image/jpeg",
    download_status: int = 200,
    extra_download_headers: dict[str, str] | None = None,
) -> httpx.MockTransport:
    """Build an httpx MockTransport that mimics Telegram's getFile + file download."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getFile"):
            return httpx.Response(
                200,
                json={"ok": True, "result": {"file_id": "abc123", "file_path": file_path}},
            )
        headers = {"content-type": download_content_type}
        if extra_download_headers:
            headers.update(extra_download_headers)
        return httpx.Response(download_status, content=download_body, headers=headers)

    return httpx.MockTransport(handler)


def _patch_client(transport: httpx.MockTransport) -> AbstractContextManager[object]:
    """Patch httpx.AsyncClient so download_telegram_media uses the mock transport."""
    real_client = httpx.AsyncClient
    return patch(
        "backend.app.media.download.httpx.AsyncClient",
        lambda *a, **kw: real_client(transport=transport),
    )


@pytest.mark.asyncio()
async def test_download_telegram_media() -> None:
    """download_telegram_media should call getFile then stream-download bytes."""
    transport = _telegram_transport()
    with _patch_client(transport):
        result = await download_telegram_media("abc123", bot_token="TOKEN")

    assert isinstance(result, DownloadedMedia)
    assert result.content == b"fake-image-bytes"
    assert result.mime_type == "image/jpeg"
    assert result.filename.endswith(".jpg")


@pytest.mark.asyncio()
async def test_download_infers_mime_from_file_path_when_octet_stream() -> None:
    """When Telegram returns application/octet-stream, infer MIME from file path."""
    transport = _telegram_transport(
        file_path="photos/file_1.jpg",
        download_content_type="application/octet-stream",
    )
    with _patch_client(transport):
        result = await download_telegram_media("abc123", bot_token="TOKEN")

    assert result.mime_type == "image/jpeg"
    assert result.filename.endswith(".jpg")


@pytest.mark.asyncio()
async def test_download_keeps_octet_stream_for_unknown_extension() -> None:
    """When extension is unrecognised, keep application/octet-stream as-is."""
    transport = _telegram_transport(
        file_path="documents/file_0.xyz",
        download_body=b"some-bytes",
        download_content_type="application/octet-stream",
    )
    with _patch_client(transport):
        result = await download_telegram_media("abc123", bot_token="TOKEN")

    assert result.mime_type == "application/octet-stream"


@pytest.mark.asyncio()
@patch("backend.app.media.download.settings")
async def test_download_rejects_oversized_streamed_body(mock_settings: object) -> None:
    """Files exceeding max_media_size_bytes mid-stream should raise ValueError."""
    mock_settings.telegram_bot_token = "TOKEN"  # type: ignore[attr-defined]
    mock_settings.http_timeout_seconds = 30.0  # type: ignore[attr-defined]
    mock_settings.max_media_size_bytes = 100  # type: ignore[attr-defined]
    mock_settings.media_download_max_seconds = 60.0  # type: ignore[attr-defined]

    transport = _telegram_transport(file_path="photos/big.jpg", download_body=b"x" * 200)
    with _patch_client(transport), pytest.raises(ValueError, match="Media file too large"):
        await download_telegram_media("abc123", bot_token="TOKEN")


@pytest.mark.asyncio()
@patch("backend.app.media.download.settings")
async def test_download_rejects_oversized_content_length(mock_settings: object) -> None:
    """Servers that volunteer a too-big Content-Length should be rejected upfront."""
    mock_settings.telegram_bot_token = "TOKEN"  # type: ignore[attr-defined]
    mock_settings.http_timeout_seconds = 30.0  # type: ignore[attr-defined]
    mock_settings.max_media_size_bytes = 100  # type: ignore[attr-defined]
    mock_settings.media_download_max_seconds = 60.0  # type: ignore[attr-defined]

    transport = _telegram_transport(
        download_body=b"x" * 50,
        extra_download_headers={"content-length": "999999"},
    )
    with _patch_client(transport), pytest.raises(ValueError, match="declared 999999"):
        await download_telegram_media("abc123", bot_token="TOKEN")


def test_parse_content_length_handles_garbage() -> None:
    """Whitespace, signs, non-numeric, and missing values must not bypass the size guard."""
    assert _parse_content_length("100") == 100
    assert _parse_content_length("  100  ") == 100
    assert _parse_content_length("") is None
    assert _parse_content_length("abc") is None
    assert _parse_content_length("-1") is None
    assert _parse_content_length("100; chunked") is None


@pytest.mark.asyncio()
async def test_download_telegram_media_error() -> None:
    """download_telegram_media should raise on HTTP error from getFile."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    with _patch_client(transport), pytest.raises(httpx.HTTPStatusError):
        await download_telegram_media("abc123", bot_token="TOKEN")


@pytest.mark.asyncio()
async def test_download_bounded_enforces_wall_time_deadline() -> None:
    """A slow-drip server that holds the connection open must be aborted."""

    async def slow_stream() -> AsyncIterator[bytes]:
        # First chunk arrives, then the stream stalls indefinitely. The
        # per-chunk httpx timeout would be reset by the trickle, so the
        # only thing that saves us is the wall-time deadline.
        yield b"x" * 8
        await asyncio.sleep(10)
        yield b"y"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=slow_stream())

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(asyncio.TimeoutError):
            await download_bounded(
                client,
                "https://example.com/slow",
                max_seconds=0.05,
                max_bytes=1024,
            )
