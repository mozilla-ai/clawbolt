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


# ---------------------------------------------------------------------------
# New service method tests: project management
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_get_project() -> None:
    service = CompanyCamService(access_token="test-token")
    project = {"id": "42", "name": "Smith Residence", "status": "active"}

    with patch("backend.app.services.companycam.httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.get = AsyncMock(return_value=_mock_response(project))
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await service.get_project("42")

    assert result.id == "42"
    assert result.name == "Smith Residence"


@pytest.mark.asyncio()
async def test_delete_project() -> None:
    service = CompanyCamService(access_token="test-token")

    with patch("backend.app.services.companycam.httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.delete = AsyncMock(return_value=_mock_response(None))
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        await service.delete_project("42")

    client.delete.assert_called_once()


@pytest.mark.asyncio()
async def test_archive_project() -> None:
    service = CompanyCamService(access_token="test-token")

    with patch("backend.app.services.companycam.httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.patch = AsyncMock(return_value=_mock_response(None))
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        await service.archive_project("42")

    client.patch.assert_called_once()


@pytest.mark.asyncio()
async def test_restore_project() -> None:
    service = CompanyCamService(access_token="test-token")

    with patch("backend.app.services.companycam.httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.put = AsyncMock(return_value=_mock_response(None))
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        await service.restore_project("42")

    client.put.assert_called_once()


@pytest.mark.asyncio()
async def test_update_notepad() -> None:
    service = CompanyCamService(access_token="test-token")

    with patch("backend.app.services.companycam.httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.put = AsyncMock(return_value=_mock_response(None))
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        await service.update_notepad("42", "Roof inspection passed")

    body = client.put.call_args.kwargs.get("json", {})
    assert body["notepad"] == "Roof inspection passed"


# ---------------------------------------------------------------------------
# New service method tests: project content
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_list_project_documents() -> None:
    service = CompanyCamService(access_token="test-token")
    docs = [{"id": "1", "name": "contract.pdf", "url": "https://example.com/c.pdf"}]

    with patch("backend.app.services.companycam.httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.get = AsyncMock(return_value=_mock_response(docs))
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await service.list_project_documents("42")

    assert len(result) == 1
    assert result[0].name == "contract.pdf"


@pytest.mark.asyncio()
async def test_list_project_documents_pagination() -> None:
    service = CompanyCamService(access_token="test-token")

    with patch("backend.app.services.companycam.httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.get = AsyncMock(return_value=_mock_response([]))
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        await service.list_project_documents("42", page=2, per_page=25)

    call_kwargs = client.get.call_args
    params = call_kwargs.kwargs.get("params", {})
    assert params["page"] == 2
    assert params["per_page"] == 25


@pytest.mark.asyncio()
async def test_list_project_comments() -> None:
    service = CompanyCamService(access_token="test-token")
    comments = [{"id": "1", "content": "Looking good", "creator_name": "John"}]

    with patch("backend.app.services.companycam.httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.get = AsyncMock(return_value=_mock_response(comments))
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await service.list_project_comments("42")

    assert len(result) == 1
    assert result[0].content == "Looking good"


@pytest.mark.asyncio()
async def test_add_project_comment() -> None:
    service = CompanyCamService(access_token="test-token")
    comment = {"id": "5", "content": "Done!", "creator_name": "Bot"}

    with patch("backend.app.services.companycam.httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.post = AsyncMock(return_value=_mock_response(comment))
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await service.add_project_comment("42", "Done!")

    assert result.id == "5"
    body = client.post.call_args.kwargs.get("json", {})
    assert body == {"comment": {"content": "Done!"}}


@pytest.mark.asyncio()
async def test_list_project_labels() -> None:
    service = CompanyCamService(access_token="test-token")
    labels = [{"id": "1", "display_value": "Roofing", "value": "roofing"}]

    with patch("backend.app.services.companycam.httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.get = AsyncMock(return_value=_mock_response(labels))
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await service.list_project_labels("42")

    assert len(result) == 1
    assert result[0].display_value == "Roofing"


@pytest.mark.asyncio()
async def test_add_project_labels() -> None:
    service = CompanyCamService(access_token="test-token")
    labels = [{"id": "2", "display_value": "Priority", "value": "priority"}]

    with patch("backend.app.services.companycam.httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.post = AsyncMock(return_value=_mock_response(labels))
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await service.add_project_labels("42", ["Priority"])

    assert len(result) == 1
    assert result[0].display_value == "Priority"
    body = client.post.call_args.kwargs.get("json", {})
    assert body == {"project": {"labels": ["Priority"]}}


# ---------------------------------------------------------------------------
# New service method tests: photo management
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_search_photos() -> None:
    service = CompanyCamService(access_token="test-token")
    photos = [{"id": "10", "uris": []}]

    with patch("backend.app.services.companycam.httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.get = AsyncMock(return_value=_mock_response(photos))
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await service.search_photos()

    assert len(result) == 1
    assert result[0].id == "10"


@pytest.mark.asyncio()
async def test_search_photos_normalize() -> None:
    """search_photos applies _normalize_photo (coordinates dict -> list)."""
    service = CompanyCamService(access_token="test-token")
    photos = [{"id": "10", "uris": [], "coordinates": {"lat": 1.0, "lon": 2.0}}]

    with patch("backend.app.services.companycam.httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.get = AsyncMock(return_value=_mock_response(photos))
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await service.search_photos()

    assert result[0].coordinates is not None
    assert isinstance(result[0].coordinates, list)


@pytest.mark.asyncio()
async def test_delete_photo() -> None:
    service = CompanyCamService(access_token="test-token")

    with patch("backend.app.services.companycam.httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.delete = AsyncMock(return_value=_mock_response(None))
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        await service.delete_photo("100")

    client.delete.assert_called_once()


@pytest.mark.asyncio()
async def test_list_photo_tags() -> None:
    service = CompanyCamService(access_token="test-token")
    tags = [{"id": "1", "display_value": "Kitchen", "value": "kitchen"}]

    with patch("backend.app.services.companycam.httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.get = AsyncMock(return_value=_mock_response(tags))
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await service.list_photo_tags("100")

    assert len(result) == 1
    assert result[0].display_value == "Kitchen"


@pytest.mark.asyncio()
async def test_add_photo_tags() -> None:
    service = CompanyCamService(access_token="test-token")
    tags = [
        {"id": "1", "display_value": "Before", "value": "before"},
        {"id": "2", "display_value": "Kitchen", "value": "kitchen"},
    ]

    with patch("backend.app.services.companycam.httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.post = AsyncMock(return_value=_mock_response(tags))
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await service.add_photo_tags("100", ["before", "kitchen"])

    assert len(result) == 2
    body = client.post.call_args.kwargs.get("json", {})
    assert body == {"tags": ["before", "kitchen"]}


@pytest.mark.asyncio()
async def test_list_photo_comments() -> None:
    service = CompanyCamService(access_token="test-token")
    comments = [{"id": "1", "content": "Nice shot"}]

    with patch("backend.app.services.companycam.httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.get = AsyncMock(return_value=_mock_response(comments))
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await service.list_photo_comments("100")

    assert len(result) == 1


@pytest.mark.asyncio()
async def test_add_photo_comment() -> None:
    service = CompanyCamService(access_token="test-token")
    comment = {"id": "3", "content": "Check this", "creator_name": "Bot"}

    with patch("backend.app.services.companycam.httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.post = AsyncMock(return_value=_mock_response(comment))
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await service.add_photo_comment("100", "Check this")

    assert result.id == "3"
    body = client.post.call_args.kwargs.get("json", {})
    assert body == {"comment": {"content": "Check this"}}


# ---------------------------------------------------------------------------
# New service method tests: checklists
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_list_checklist_templates() -> None:
    service = CompanyCamService(access_token="test-token")
    templates = [{"id": "1", "name": "Roof Inspection"}]

    with patch("backend.app.services.companycam.httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.get = AsyncMock(return_value=_mock_response(templates))
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await service.list_checklist_templates()

    assert len(result) == 1
    assert result[0].name == "Roof Inspection"


@pytest.mark.asyncio()
async def test_list_project_checklists() -> None:
    service = CompanyCamService(access_token="test-token")
    checklists = [{"id": "10", "name": "Inspection", "project_id": "42"}]

    with patch("backend.app.services.companycam.httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.get = AsyncMock(return_value=_mock_response(checklists))
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await service.list_project_checklists("42")

    assert len(result) == 1
    assert result[0].name == "Inspection"


@pytest.mark.asyncio()
async def test_create_project_checklist() -> None:
    service = CompanyCamService(access_token="test-token")
    checklist = {"id": "20", "name": "Roof Survey", "project_id": "42"}

    with patch("backend.app.services.companycam.httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.post = AsyncMock(return_value=_mock_response(checklist))
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await service.create_project_checklist("42", "tmpl-1")

    assert result.id == "20"
    body = client.post.call_args.kwargs.get("json", {})
    assert body["checklist_template_id"] == "tmpl-1"


@pytest.mark.asyncio()
async def test_get_checklist() -> None:
    service = CompanyCamService(access_token="test-token")
    checklist = {
        "id": "20",
        "name": "Roof Survey",
        "sections": [
            {
                "id": "s1",
                "title": "Exterior",
                "tasks": [
                    {"id": "t1", "title": "Check shingles", "completed_at": 1234},
                    {"id": "t2", "title": "Check gutters", "completed_at": None},
                ],
            }
        ],
        "sectionless_tasks": [],
    }

    with patch("backend.app.services.companycam.httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.get = AsyncMock(return_value=_mock_response(checklist))
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await service.get_checklist("42", "20")

    assert result.id == "20"
    assert result.sections is not None
    assert len(result.sections) == 1
    assert len(result.sections[0].tasks or []) == 2


# ---------------------------------------------------------------------------
# New service method tests: pagination on existing methods
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_search_projects_pagination() -> None:
    service = CompanyCamService(access_token="test-token")

    with patch("backend.app.services.companycam.httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.get = AsyncMock(return_value=_mock_response([]))
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        await service.search_projects("test", page=3, per_page=10)

    params = client.get.call_args.kwargs.get("params", {})
    assert params["page"] == 3
    assert params["per_page"] == 10


@pytest.mark.asyncio()
async def test_list_project_photos_pagination() -> None:
    service = CompanyCamService(access_token="test-token")

    with patch("backend.app.services.companycam.httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.get = AsyncMock(return_value=_mock_response([]))
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        await service.list_project_photos("42", page=2, per_page=25)

    params = client.get.call_args.kwargs.get("params", {})
    assert params["page"] == 2
    assert params["per_page"] == 25


# ---------------------------------------------------------------------------
# New service method tests: error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_get_project_404() -> None:
    service = CompanyCamService(access_token="test-token")

    with patch("backend.app.services.companycam.httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.get = AsyncMock(return_value=_mock_response(None, status_code=404))
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(httpx.HTTPStatusError):
            await service.get_project("nonexistent")


@pytest.mark.asyncio()
async def test_delete_photo_404() -> None:
    service = CompanyCamService(access_token="test-token")

    with patch("backend.app.services.companycam.httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.delete = AsyncMock(return_value=_mock_response(None, status_code=404))
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        with pytest.raises(httpx.HTTPStatusError):
            await service.delete_photo("nonexistent")


# ---------------------------------------------------------------------------
# New tool registration tests
# ---------------------------------------------------------------------------


def test_new_companycam_tools_registered() -> None:
    """All new CompanyCam SubToolInfo entries should be registered."""
    from backend.app.agent.tools.names import ToolName
    from backend.app.agent.tools.registry import default_registry, ensure_tool_modules_imported

    ensure_tool_modules_imported()
    info = default_registry._factories["companycam"]
    tool_names = {st.name for st in info.sub_tools}

    expected = {
        ToolName.COMPANYCAM_CONNECT,
        ToolName.COMPANYCAM_SEARCH_PROJECTS,
        ToolName.COMPANYCAM_CREATE_PROJECT,
        ToolName.COMPANYCAM_UPDATE_PROJECT,
        ToolName.COMPANYCAM_UPLOAD_PHOTO,
        ToolName.COMPANYCAM_GET_PROJECT,
        ToolName.COMPANYCAM_ARCHIVE_PROJECT,
        ToolName.COMPANYCAM_DELETE_PROJECT,
        ToolName.COMPANYCAM_UPDATE_NOTEPAD,
        ToolName.COMPANYCAM_LIST_DOCUMENTS,
        ToolName.COMPANYCAM_ADD_COMMENT,
        ToolName.COMPANYCAM_LIST_COMMENTS,
        ToolName.COMPANYCAM_TAG_PHOTO,
        ToolName.COMPANYCAM_DELETE_PHOTO,
        ToolName.COMPANYCAM_SEARCH_PHOTOS,
        ToolName.COMPANYCAM_LIST_CHECKLISTS,
        ToolName.COMPANYCAM_GET_CHECKLIST,
        ToolName.COMPANYCAM_CREATE_CHECKLIST,
    }

    assert expected == tool_names


def test_new_tool_permissions() -> None:
    """Read-only tools should be 'always', write tools should be 'ask'."""
    from backend.app.agent.tools.names import ToolName
    from backend.app.agent.tools.registry import default_registry, ensure_tool_modules_imported

    ensure_tool_modules_imported()
    info = default_registry._factories["companycam"]
    perms = {st.name: st.default_permission for st in info.sub_tools}

    # Read-only tools should be "always"
    for name in [
        ToolName.COMPANYCAM_GET_PROJECT,
        ToolName.COMPANYCAM_LIST_DOCUMENTS,
        ToolName.COMPANYCAM_LIST_COMMENTS,
        ToolName.COMPANYCAM_SEARCH_PHOTOS,
        ToolName.COMPANYCAM_LIST_CHECKLISTS,
        ToolName.COMPANYCAM_GET_CHECKLIST,
    ]:
        assert perms[name] == "always", f"{name} should be 'always'"

    # Write tools should be "ask"
    for name in [
        ToolName.COMPANYCAM_ARCHIVE_PROJECT,
        ToolName.COMPANYCAM_DELETE_PROJECT,
        ToolName.COMPANYCAM_UPDATE_NOTEPAD,
        ToolName.COMPANYCAM_ADD_COMMENT,
        ToolName.COMPANYCAM_TAG_PHOTO,
        ToolName.COMPANYCAM_DELETE_PHOTO,
        ToolName.COMPANYCAM_CREATE_CHECKLIST,
    ]:
        assert perms[name] == "ask", f"{name} should be 'ask'"
