from unittest.mock import AsyncMock, patch

import httpx
import pytest

from backend.app.media.download import (
    DownloadedMedia,
    classify_media,
    download_twilio_media,
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
async def test_download_twilio_media() -> None:
    """download_twilio_media should fetch bytes with auth."""
    mock_response = httpx.Response(
        200,
        content=b"fake-image-bytes",
        headers={"content-type": "image/jpeg"},
        request=httpx.Request("GET", "https://api.twilio.com/media/test.jpg"),
    )

    with patch("backend.app.media.download.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_cls.return_value = mock_client

        result = await download_twilio_media(
            "https://api.twilio.com/media/test.jpg",
            twilio_auth=("AC123", "authtoken"),
        )

    assert isinstance(result, DownloadedMedia)
    assert result.content == b"fake-image-bytes"
    assert result.mime_type == "image/jpeg"
    assert result.filename.endswith(".jpg")
    mock_client.get.assert_called_once()
    call_kwargs = mock_client.get.call_args
    assert call_kwargs[1]["auth"] == ("AC123", "authtoken")


@pytest.mark.asyncio()
async def test_download_twilio_media_error() -> None:
    """download_twilio_media should raise on HTTP error."""
    mock_response = httpx.Response(
        404,
        request=httpx.Request("GET", "https://api.twilio.com/media/missing.jpg"),
    )

    with patch("backend.app.media.download.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_cls.return_value = mock_client

        with pytest.raises(httpx.HTTPStatusError):
            await download_twilio_media(
                "https://api.twilio.com/media/missing.jpg",
                twilio_auth=("AC123", "authtoken"),
            )
