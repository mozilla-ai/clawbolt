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


def test_classify_audio_types() -> None:
    assert classify_media("audio/ogg") == "audio"
    assert classify_media("audio/mp3") == "audio"
    assert classify_media("audio/amr") == "audio"


def test_classify_video_types() -> None:
    assert classify_media("video/mp4") == "video"
    assert classify_media("video/3gpp") == "video"


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
