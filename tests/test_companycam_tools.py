"""Tests for the CompanyCam integration tools."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from backend.app.services.companycam import CompanyCamService, get_photo_url

# ---------------------------------------------------------------------------
# CompanyCamService tests
# ---------------------------------------------------------------------------


def test_service_requires_token() -> None:
    with pytest.raises(ValueError, match="access token is required"):
        CompanyCamService(access_token="")


def test_service_accepts_valid_token() -> None:
    s = CompanyCamService(access_token="valid-token")
    assert s._access_token == "valid-token"


def _mock_response(json_data: object, status_code: int = 200) -> httpx.Response:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=MagicMock(), response=resp
        )
    return resp


@pytest.mark.asyncio()
async def test_validate_token() -> None:
    service = CompanyCamService(access_token="test-token")
    user_data = {"id": "1", "first_name": "John", "email_address": "john@example.com"}

    with patch("backend.app.services.companycam.httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.get = AsyncMock(return_value=_mock_response(user_data))
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await service.validate_token()

    assert result.first_name == "John"


@pytest.mark.asyncio()
async def test_search_projects() -> None:
    service = CompanyCamService(access_token="test-token")
    projects = [{"id": "42", "name": "Smith Residence"}]

    with patch("backend.app.services.companycam.httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.get = AsyncMock(return_value=_mock_response(projects))
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await service.search_projects("Smith")

    assert len(result) == 1
    assert result[0].id == "42"


@pytest.mark.asyncio()
async def test_create_project() -> None:
    service = CompanyCamService(access_token="test-token")
    created = {"id": "99", "name": "New Project"}

    with patch("backend.app.services.companycam.httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.post = AsyncMock(return_value=_mock_response(created))
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await service.create_project("New Project", "123 Main St")

    assert result.id == "99"


@pytest.mark.asyncio()
async def test_upload_photo() -> None:
    service = CompanyCamService(access_token="test-token")
    photo = {
        "id": "100",
        "uris": [{"type": "original", "uri": "https://photos.cc.com/abc.jpg"}],
    }

    with patch("backend.app.services.companycam.httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.post = AsyncMock(return_value=_mock_response(photo))
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await service.upload_photo(
            project_id="42",
            photo_uri="https://example.com/photo.jpg",
            tags=["kitchen", "demo"],
            description="Kitchen demo",
        )

    assert result.id == "100"
    call_kwargs = client.post.call_args
    body = call_kwargs.kwargs.get("json", {})
    assert body["photo"]["uri"] == "https://example.com/photo.jpg"
    assert body["photo"]["description"] == "Kitchen demo"
    assert body["photo"]["tags"] == ["kitchen", "demo"]


@pytest.mark.asyncio()
async def test_list_project_photos() -> None:
    service = CompanyCamService(access_token="test-token")
    photos = [{"id": "10", "uris": []}]

    with patch("backend.app.services.companycam.httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.get = AsyncMock(return_value=_mock_response(photos))
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await service.list_project_photos("42")

    assert len(result) == 1


# ---------------------------------------------------------------------------
# get_photo_url tests
# ---------------------------------------------------------------------------


def test_get_photo_url_original() -> None:
    from backend.app.services.companycam_models import ImageURI, Photo

    photo = Photo(id="1", uris=[ImageURI(type="original", uri="https://cc.com/a.jpg")])
    assert get_photo_url(photo) == "https://cc.com/a.jpg"


def test_get_photo_url_fallback() -> None:
    from backend.app.services.companycam_models import ImageURI, Photo

    photo = Photo(id="1", uris=[ImageURI(type="thumb", uri="https://cc.com/thumb.jpg")])
    assert get_photo_url(photo) == "https://cc.com/thumb.jpg"


def test_get_photo_url_no_uris() -> None:
    from backend.app.services.companycam_models import Photo

    photo = Photo(id="42", uris=[])
    assert "42" in get_photo_url(photo)


# ---------------------------------------------------------------------------
# Tool registration tests
# ---------------------------------------------------------------------------


def test_companycam_tools_registered() -> None:
    """CompanyCam tools should be registered in the default registry."""
    from backend.app.agent.tools.registry import default_registry, ensure_tool_modules_imported

    ensure_tool_modules_imported()
    assert "companycam" in default_registry.factory_names


def test_companycam_auth_check_no_token() -> None:
    """Auth check should return a reason when no token is stored and no env var."""
    from backend.app.agent.tools.companycam_tools import _companycam_auth_check
    from backend.app.config import settings

    user = MagicMock()
    user.id = "test-user-no-token"
    ctx = MagicMock()
    ctx.user = user

    original = settings.companycam_access_token
    try:
        settings.companycam_access_token = ""
        with patch("backend.app.agent.tools.companycam_tools.oauth_service") as mock_oauth:
            mock_oauth.load_token.return_value = None
            result = _companycam_auth_check(ctx)
    finally:
        settings.companycam_access_token = original

    assert result is not None
    assert "not connected" in result.lower()


def test_companycam_auth_check_with_token() -> None:
    """Auth check should return None when a token is stored."""
    from backend.app.agent.tools.companycam_tools import _companycam_auth_check

    user = MagicMock()
    user.id = "test-user-with-token"
    ctx = MagicMock()
    ctx.user = user

    with patch("backend.app.agent.tools.companycam_tools.oauth_service") as mock_oauth:
        token = MagicMock()
        token.access_token = "valid-token"
        mock_oauth.load_token.return_value = token
        result = _companycam_auth_check(ctx)

    assert result is None
