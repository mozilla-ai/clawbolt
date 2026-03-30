from unittest.mock import AsyncMock, patch

import httpx
import pytest

from backend.app.media.download import (
    DownloadedMedia,
    classify_media,
    download_telegram_media,
)


def test_classify_image_types() -> None:
    assert classify_media("image/jpeg") == "image"
    assert classify_media("image/png") == "image"
    assert classify_media("image/gif") == "image"


def test_classify_audio_treated_as_unknown() -> None:
    """Audio MIME types should be classified as unknown (voice memos removed)."""
    assert classify_media("audio/ogg") == "unknown"
    assert classify_media("audio/mp3") == "unknown"
    assert classify_media("audio/amr") == "unknown"


def test_classify_video_treated_as_unknown() -> None:
    """Video MIME types should be classified as unknown (video support removed)."""
    assert classify_media("video/mp4") == "unknown"
    assert classify_media("video/3gpp") == "unknown"


def test_classify_pdf() -> None:
    assert classify_media("application/pdf") == "pdf"


def test_classify_unknown() -> None:
    assert classify_media("application/octet-stream") == "unknown"
    assert classify_media("text/plain") == "unknown"


@pytest.mark.asyncio()
async def test_download_telegram_media() -> None:
    """download_telegram_media should call getFile API then download bytes."""
    get_file_response = httpx.Response(
        200,
        json={"ok": True, "result": {"file_id": "abc123", "file_path": "photos/file_0.jpg"}},
        request=httpx.Request("GET", "https://api.telegram.org/botTOKEN/getFile"),
    )
    download_response = httpx.Response(
        200,
        content=b"fake-image-bytes",
        headers={"content-type": "image/jpeg"},
        request=httpx.Request("GET", "https://api.telegram.org/file/botTOKEN/photos/file_0.jpg"),
    )

    with patch("backend.app.media.download.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get.side_effect = [get_file_response, download_response]
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_cls.return_value = mock_client

        result = await download_telegram_media("abc123", bot_token="TOKEN")

    assert isinstance(result, DownloadedMedia)
    assert result.content == b"fake-image-bytes"
    assert result.mime_type == "image/jpeg"
    assert result.filename.endswith(".jpg")
    assert mock_client.get.call_count == 2


@pytest.mark.asyncio()
async def test_download_infers_mime_from_file_path_when_octet_stream() -> None:
    """When Telegram returns application/octet-stream, infer MIME from file path extension."""
    get_file_response = httpx.Response(
        200,
        json={"ok": True, "result": {"file_id": "abc123", "file_path": "photos/file_1.jpg"}},
        request=httpx.Request("GET", "https://api.telegram.org/botTOKEN/getFile"),
    )
    download_response = httpx.Response(
        200,
        content=b"fake-image-bytes",
        headers={"content-type": "application/octet-stream"},
        request=httpx.Request("GET", "https://api.telegram.org/file/botTOKEN/photos/file_1.jpg"),
    )

    with patch("backend.app.media.download.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get.side_effect = [get_file_response, download_response]
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_cls.return_value = mock_client

        result = await download_telegram_media("abc123", bot_token="TOKEN")

    assert result.mime_type == "image/jpeg"
    assert result.filename.endswith(".jpg")


@pytest.mark.asyncio()
async def test_download_keeps_octet_stream_for_unknown_extension() -> None:
    """When extension is unrecognised, keep application/octet-stream as-is."""
    get_file_response = httpx.Response(
        200,
        json={"ok": True, "result": {"file_id": "abc123", "file_path": "documents/file_0.xyz"}},
        request=httpx.Request("GET", "https://api.telegram.org/botTOKEN/getFile"),
    )
    download_response = httpx.Response(
        200,
        content=b"some-bytes",
        headers={"content-type": "application/octet-stream"},
        request=httpx.Request("GET", "https://api.telegram.org/file/botTOKEN/documents/file_0.xyz"),
    )

    with patch("backend.app.media.download.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get.side_effect = [get_file_response, download_response]
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_cls.return_value = mock_client

        result = await download_telegram_media("abc123", bot_token="TOKEN")

    assert result.mime_type == "application/octet-stream"


@pytest.mark.asyncio()
@patch("backend.app.media.download.settings")
async def test_download_rejects_oversized_media(mock_settings: object) -> None:
    """Files exceeding max_media_size_bytes should raise ValueError."""
    mock_settings.telegram_bot_token = "TOKEN"  # type: ignore[attr-defined]
    mock_settings.http_timeout_seconds = 30.0  # type: ignore[attr-defined]
    mock_settings.max_media_size_bytes = 100  # type: ignore[attr-defined]

    get_file_response = httpx.Response(
        200,
        json={"ok": True, "result": {"file_id": "abc123", "file_path": "photos/big.jpg"}},
        request=httpx.Request("GET", "https://api.telegram.org/botTOKEN/getFile"),
    )
    # 200 bytes > 100 byte limit
    download_response = httpx.Response(
        200,
        content=b"x" * 200,
        headers={"content-type": "image/jpeg"},
        request=httpx.Request("GET", "https://api.telegram.org/file/botTOKEN/photos/big.jpg"),
    )

    with patch("backend.app.media.download.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get.side_effect = [get_file_response, download_response]
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_cls.return_value = mock_client

        with pytest.raises(ValueError, match="Media file too large"):
            await download_telegram_media("abc123", bot_token="TOKEN")


@pytest.mark.asyncio()
async def test_download_telegram_media_error() -> None:
    """download_telegram_media should raise on HTTP error."""
    error_response = httpx.Response(
        404,
        request=httpx.Request("GET", "https://api.telegram.org/botTOKEN/getFile"),
    )

    with patch("backend.app.media.download.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get.return_value = error_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_cls.return_value = mock_client

        with pytest.raises(httpx.HTTPStatusError):
            await download_telegram_media("abc123", bot_token="TOKEN")
