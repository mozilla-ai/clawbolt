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
async def test_create_project_with_null_integration_relation_id() -> None:
    """Regression: CompanyCam API returns integrations with relation_id=null."""
    service = CompanyCamService(access_token="test-token")
    created = {
        "id": "99",
        "name": "New Project",
        "integrations": [{"type": "Clawbolt", "relation_id": None}],
    }

    with patch("backend.app.services.companycam.httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.post = AsyncMock(return_value=_mock_response(created))
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=client)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        result = await service.create_project("New Project", "123 Main St")

    assert result.id == "99"
    assert result.integrations is not None
    assert result.integrations[0].relation_id is None


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


def test_get_photo_url_null_uri_skipped() -> None:
    """Regression: ImageURI with null uri should be skipped."""
    from backend.app.services.companycam_models import ImageURI, Photo

    photo = Photo(
        id="1",
        uris=[
            ImageURI(type="original", uri=None),
            ImageURI(type="web", uri="https://cc.com/web.jpg"),
        ],
    )
    assert get_photo_url(photo) == "https://cc.com/web.jpg"


def test_get_photo_url_all_null_uris_fallback() -> None:
    """Regression: if all ImageURI.uri are null, fall back to API URL."""
    from backend.app.services.companycam_models import ImageURI, Photo

    photo = Photo(id="42", uris=[ImageURI(type="original")])
    assert "42" in get_photo_url(photo)


# ---------------------------------------------------------------------------
# Tool registration tests
# ---------------------------------------------------------------------------


def test_companycam_tools_registered() -> None:
    """CompanyCam tools should be registered in the default registry."""
    from backend.app.agent.tools.registry import default_registry, ensure_tool_modules_imported

    ensure_tool_modules_imported()
    assert "companycam" in default_registry.factory_names


def test_companycam_auth_check_not_connected() -> None:
    """Auth check should return a reason when OAuth is not connected."""
    from backend.app.agent.tools.companycam_tools import _companycam_auth_check
    from backend.app.config import settings

    user = MagicMock()
    user.id = "test-user-no-token"
    ctx = MagicMock()
    ctx.user = user

    with (
        patch.object(settings, "companycam_client_id", "cid"),
        patch.object(settings, "companycam_client_secret", "csec"),
        patch("backend.app.agent.tools.companycam_tools.oauth_service") as mock_oauth,
    ):
        mock_oauth.is_connected.return_value = False
        result = _companycam_auth_check(ctx)

    assert result is not None
    assert "not connected" in result.lower()
    assert "manage_integration" in result


def test_companycam_auth_check_connected() -> None:
    """Auth check should return None when OAuth is connected."""
    from backend.app.agent.tools.companycam_tools import _companycam_auth_check
    from backend.app.config import settings

    user = MagicMock()
    user.id = "test-user-connected"
    ctx = MagicMock()
    ctx.user = user

    with (
        patch.object(settings, "companycam_client_id", "cid"),
        patch.object(settings, "companycam_client_secret", "csec"),
        patch("backend.app.agent.tools.companycam_tools.oauth_service") as mock_oauth,
    ):
        mock_oauth.is_connected.return_value = True
        result = _companycam_auth_check(ctx)

    assert result is None


def test_companycam_auth_check_not_configured() -> None:
    """Auth check should return None (hide tools) when OAuth creds are not configured."""
    from backend.app.agent.tools.companycam_tools import _companycam_auth_check
    from backend.app.config import settings

    user = MagicMock()
    user.id = "test-user"
    ctx = MagicMock()
    ctx.user = user

    with (
        patch.object(settings, "companycam_client_id", ""),
        patch.object(settings, "companycam_client_secret", ""),
    ):
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


# ---------------------------------------------------------------------------
# Receipt shape (contractor-facing iMessage footer)
# ---------------------------------------------------------------------------
#
# Every write-side CompanyCam tool populates a ToolReceipt(action,
# target, url). The footer rendered to iMessage/SMS must never surface
# raw CompanyCam ids (8-digit numeric strings) because a contractor
# cannot recognize or click them. These tests assert the invariant
# per-tool: no digit-only id substring in the target, and a clickable
# URL for every non-destructive action.

import re  # noqa: E402 — placed with the receipt-shape block for locality
from typing import Any  # noqa: E402

from backend.app.agent.tools.base import ToolReceipt  # noqa: E402
from backend.app.agent.tools.companycam_checklists import build_checklist_tools  # noqa: E402
from backend.app.agent.tools.companycam_photos import build_photo_tools  # noqa: E402
from backend.app.agent.tools.companycam_projects import build_project_tools  # noqa: E402

# Matches any 6+ digit run with word boundaries — catches both a bare
# id ("94772883") and an id embedded in a longer string
# ("kitchen 94772883 done"). Using \b ensures a short numeric token
# like "2026" inside a date does not false-flag, but 6+ digits is
# unambiguously a CompanyCam id.
_RAW_ID_RE = re.compile(r"\b\d{6,}\b")


def _assert_receipt_clean(receipt: ToolReceipt | None, *, expect_url: bool) -> None:
    assert receipt is not None, "write-side tool must populate a receipt"
    assert receipt.action, "receipt action must be non-empty"
    assert receipt.target, "receipt target must be non-empty"
    # No raw CompanyCam id in the visible text.
    assert _RAW_ID_RE.search(receipt.target) is None, (
        f"receipt target contains raw id: {receipt.target!r}"
    )
    # No control chars that could forge a receipt line.
    assert "\n" not in receipt.target, "receipt target contains newline"
    assert "\r" not in receipt.target, "receipt target contains carriage return"
    assert "\t" not in receipt.target, "receipt target contains tab"
    # Non-destructive actions must have a clickable URL.
    if expect_url:
        assert receipt.url, "non-destructive action must supply a URL"
        assert receipt.url.startswith("https://app.companycam.com/"), (
            f"URL must point at CompanyCam web app: {receipt.url!r}"
        )
    else:
        assert receipt.url is None, f"destructive action should not have a URL: {receipt.url!r}"


def _get_tool(tools: list, name: str) -> Any:
    for t in tools:
        if t.name == name:
            return t
    raise AssertionError(f"tool {name} not found")


@pytest.mark.asyncio()
async def test_receipt_create_project_is_clean() -> None:
    """Receipt for create_project: human name, full URL, no raw id."""
    from backend.app.agent.tools.names import ToolName

    service = MagicMock(spec=CompanyCamService)
    project_data = {
        "id": "94772883",
        "name": "Smith Residence",
        "project_url": "https://app.companycam.com/projects/94772883",
    }
    from backend.app.services.companycam_models import Project

    service.create_project = AsyncMock(return_value=Project.model_validate(project_data))
    tool = _get_tool(build_project_tools(service), ToolName.COMPANYCAM_CREATE_PROJECT)
    result = await tool.function(name="Smith Residence", address="")
    _assert_receipt_clean(result.receipt, expect_url=True)
    assert result.receipt.target == "Smith Residence"


@pytest.mark.asyncio()
async def test_receipt_archive_project_is_clean() -> None:
    """Receipt for archive_project (void return) uses generic target + URL from id."""
    from backend.app.agent.tools.names import ToolName

    service = MagicMock(spec=CompanyCamService)
    service.archive_project = AsyncMock(return_value=None)
    tool = _get_tool(build_project_tools(service), ToolName.COMPANYCAM_ARCHIVE_PROJECT)
    result = await tool.function(project_id="94772883")
    _assert_receipt_clean(result.receipt, expect_url=True)
    assert result.receipt.target == "project"


@pytest.mark.asyncio()
async def test_receipt_delete_project_is_clean_no_url() -> None:
    """Delete receipt: generic target, no URL (entity is gone)."""
    from backend.app.agent.tools.names import ToolName

    service = MagicMock(spec=CompanyCamService)
    service.delete_project = AsyncMock(return_value=None)
    tool = _get_tool(build_project_tools(service), ToolName.COMPANYCAM_DELETE_PROJECT)
    result = await tool.function(project_id="94772883")
    _assert_receipt_clean(result.receipt, expect_url=False)
    assert result.receipt.target == "project"


@pytest.mark.asyncio()
async def test_receipt_update_notepad_is_clean() -> None:
    from backend.app.agent.tools.names import ToolName

    service = MagicMock(spec=CompanyCamService)
    service.update_notepad = AsyncMock(return_value=None)
    tool = _get_tool(build_project_tools(service), ToolName.COMPANYCAM_UPDATE_NOTEPAD)
    result = await tool.function(project_id="94772883", notepad="On track")
    _assert_receipt_clean(result.receipt, expect_url=True)


@pytest.mark.asyncio()
async def test_receipt_add_comment_uses_parent_url() -> None:
    """Comments have no own URL. The receipt links to the parent entity."""
    from backend.app.agent.tools.names import ToolName

    service = MagicMock(spec=CompanyCamService)
    from backend.app.services.companycam_models import Comment

    service.add_project_comment = AsyncMock(
        return_value=Comment.model_validate({"id": "555", "content": "All demo done"})
    )
    ctx = MagicMock()
    tool = _get_tool(build_photo_tools(service, ctx), ToolName.COMPANYCAM_ADD_COMMENT)
    result = await tool.function(
        target_type="project", target_id="388472672", content="All demo done"
    )
    _assert_receipt_clean(result.receipt, expect_url=True)
    assert result.receipt.target == "All demo done"
    assert "projects/388472672" in result.receipt.url


@pytest.mark.asyncio()
async def test_receipt_add_comment_truncates_long_content() -> None:
    """A 500-char comment never reaches the iMessage footer intact."""
    from backend.app.agent.tools.names import ToolName

    service = MagicMock(spec=CompanyCamService)
    from backend.app.services.companycam_models import Comment

    service.add_photo_comment = AsyncMock(
        return_value=Comment.model_validate({"id": "555", "content": "X"})
    )
    ctx = MagicMock()
    tool = _get_tool(build_photo_tools(service, ctx), ToolName.COMPANYCAM_ADD_COMMENT)
    long_content = "A" * 500
    result = await tool.function(target_type="photo", target_id="8675309", content=long_content)
    _assert_receipt_clean(result.receipt, expect_url=True)
    assert len(result.receipt.target) <= 40
    assert result.receipt.target.endswith("\u2026")


@pytest.mark.asyncio()
async def test_receipt_tag_photo_is_clean() -> None:
    from backend.app.agent.tools.names import ToolName

    service = MagicMock(spec=CompanyCamService)
    from backend.app.services.companycam_models import Tag

    service.add_photo_tags = AsyncMock(
        return_value=[
            Tag.model_validate({"id": "1", "display_value": "kitchen", "value": "kitchen"}),
            Tag.model_validate({"id": "2", "display_value": "demo", "value": "demo"}),
        ]
    )
    ctx = MagicMock()
    tool = _get_tool(build_photo_tools(service, ctx), ToolName.COMPANYCAM_TAG_PHOTO)
    result = await tool.function(photo_id="8675309", tags=["kitchen", "demo"])
    _assert_receipt_clean(result.receipt, expect_url=True)
    assert result.receipt.target == "kitchen, demo"


@pytest.mark.asyncio()
async def test_receipt_delete_photo_is_clean_no_url() -> None:
    from backend.app.agent.tools.names import ToolName

    service = MagicMock(spec=CompanyCamService)
    service.delete_photo = AsyncMock(return_value=None)
    ctx = MagicMock()
    tool = _get_tool(build_photo_tools(service, ctx), ToolName.COMPANYCAM_DELETE_PHOTO)
    result = await tool.function(photo_id="8675309")
    _assert_receipt_clean(result.receipt, expect_url=False)
    assert result.receipt.target == "photo"


@pytest.mark.asyncio()
async def test_receipt_create_checklist_is_clean() -> None:
    from backend.app.agent.tools.names import ToolName

    service = MagicMock(spec=CompanyCamService)
    from backend.app.services.companycam_models import Checklist

    service.create_project_checklist = AsyncMock(
        return_value=Checklist.model_validate(
            {"id": "777", "name": "Rough-in inspection", "completed_at": None}
        )
    )
    tool = _get_tool(build_checklist_tools(service), ToolName.COMPANYCAM_CREATE_CHECKLIST)
    result = await tool.function(project_id="94772883", template_id="abc")
    _assert_receipt_clean(result.receipt, expect_url=True)
    assert result.receipt.target == "Rough-in inspection"


@pytest.mark.asyncio()
async def test_receipt_upload_photo_uses_app_url() -> None:
    """Regression: upload receipt must link to the app, not the CDN image."""
    from backend.app.agent.tools.names import ToolName
    from backend.app.media.download import DownloadedMedia
    from backend.app.services.companycam_models import ImageURI, Photo

    service = MagicMock(spec=CompanyCamService)
    photo_obj = Photo(
        id="39951388",
        description="Clock repair job site",
        processing_status="processed",
        uris=[ImageURI(type="original", uri="https://img.companycam.com/long-cdn-hash.jpg")],
    )
    service.upload_photo = AsyncMock(return_value=photo_obj)

    ctx = MagicMock()
    ctx.user.id = "test-user"
    ctx.downloaded_media = [
        DownloadedMedia(
            content=b"fake-jpg",
            mime_type="image/jpeg",
            original_url="https://example.com/photo.jpg",
            filename="photo.jpg",
        )
    ]

    with (
        patch(
            "backend.app.services.webhook.discover_tunnel_url",
            new_callable=AsyncMock,
            return_value="https://tunnel.example.com",
        ),
        patch(
            "backend.app.routers.media_temp.create_temp_media_url",
            return_value="https://tunnel.example.com/media/tmp/abc",
        ),
    ):
        tool = _get_tool(build_photo_tools(service, ctx), ToolName.COMPANYCAM_UPLOAD_PHOTO)
        result = await tool.function(project_id="94772883")

    _assert_receipt_clean(result.receipt, expect_url=True)
    assert "photos/39951388" in result.receipt.url
    # Content shown to the LLM should also use the app URL, not the CDN URL
    assert "img.companycam.com" not in result.content


@pytest.mark.asyncio()
async def test_receipt_upload_duplicate_photo_uses_app_url() -> None:
    """Regression: duplicate-photo receipt must link to the app, not the CDN."""
    from backend.app.agent.tools.names import ToolName
    from backend.app.media.download import DownloadedMedia
    from backend.app.services.companycam_models import ImageURI, Photo

    service = MagicMock(spec=CompanyCamService)
    photo_obj = Photo(
        id="39951388",
        description="Kitchen demo",
        processing_status="duplicate",
        uris=[ImageURI(type="original", uri="https://img.companycam.com/dup.jpg")],
    )
    service.upload_photo = AsyncMock(return_value=photo_obj)

    ctx = MagicMock()
    ctx.user.id = "test-user"
    ctx.downloaded_media = [
        DownloadedMedia(
            content=b"fake-jpg",
            mime_type="image/jpeg",
            original_url="https://example.com/photo.jpg",
            filename="photo.jpg",
        )
    ]

    with (
        patch(
            "backend.app.services.webhook.discover_tunnel_url",
            new_callable=AsyncMock,
            return_value="https://tunnel.example.com",
        ),
        patch(
            "backend.app.routers.media_temp.create_temp_media_url",
            return_value="https://tunnel.example.com/media/tmp/abc",
        ),
    ):
        tool = _get_tool(build_photo_tools(service, ctx), ToolName.COMPANYCAM_UPLOAD_PHOTO)
        result = await tool.function(project_id="94772883")

    _assert_receipt_clean(result.receipt, expect_url=True)
    assert "photos/39951388" in result.receipt.url


@pytest.mark.asyncio()
async def test_upload_photo_strips_dict_description() -> None:
    """Regression: LLM may pass a dict repr as the photo description.
    The tool must strip it before sending to CompanyCam so the project
    doesn't store garbage."""
    from backend.app.agent.tools.names import ToolName
    from backend.app.media.download import DownloadedMedia
    from backend.app.services.companycam_models import ImageURI, Photo

    service = MagicMock(spec=CompanyCamService)
    photo_obj = Photo(
        id="39951388",
        description="",
        processing_status="processed",
        uris=[ImageURI(type="original", uri="https://img.companycam.com/abc.jpg")],
    )
    service.upload_photo = AsyncMock(return_value=photo_obj)

    ctx = MagicMock()
    ctx.user.id = "test-user"
    ctx.downloaded_media = [
        DownloadedMedia(
            content=b"fake-jpg",
            mime_type="image/jpeg",
            original_url="https://example.com/photo.jpg",
            filename="photo.jpg",
        )
    ]

    dict_description = "{'id': '39959882', 'html_content': 'Basement staircase'}"
    with (
        patch(
            "backend.app.services.webhook.discover_tunnel_url",
            new_callable=AsyncMock,
            return_value="https://tunnel.example.com",
        ),
        patch(
            "backend.app.routers.media_temp.create_temp_media_url",
            return_value="https://tunnel.example.com/media/tmp/abc",
        ),
    ):
        tool = _get_tool(build_photo_tools(service, ctx), ToolName.COMPANYCAM_UPLOAD_PHOTO)
        await tool.function(project_id="94772883", description=dict_description)

    # The description sent to CompanyCam must be empty, not the dict repr
    call_kwargs = service.upload_photo.call_args
    assert call_kwargs.kwargs.get("description", "") == ""


@pytest.mark.asyncio()
async def test_receipt_rendered_output_has_no_raw_ids() -> None:
    """End-to-end: a grouped footer of five actions on one project
    never surfaces a raw CompanyCam id in the rendered output."""
    from backend.app.agent.context import StoredToolInteraction, StoredToolReceipt
    from backend.app.agent.tool_summary import format_receipts_block

    # Simulate the receipt fingerprints that each CompanyCam tool would
    # leave after a successful call (matches the tool-layer rewrite).
    fake_calls = [
        StoredToolInteraction(
            tool_call_id=f"cc-{i}",
            name=name,
            args={},
            result="",
            is_error=False,
            receipt=StoredToolReceipt(action=action, target=target, url=url),
        )
        for i, (name, action, target, url) in enumerate(
            [
                (
                    "companycam_create_project",
                    "Created CompanyCam project",
                    "Smith Residence",
                    "https://app.companycam.com/projects/94772883/photos",
                ),
                (
                    "companycam_update_notepad",
                    "Updated notepad on CompanyCam project",
                    "project",
                    "https://app.companycam.com/projects/94772883/photos",
                ),
                (
                    "companycam_add_comment",
                    "Commented on CompanyCam project",
                    "All demo done",
                    "https://app.companycam.com/projects/94772883/photos",
                ),
                (
                    "companycam_tag_photo",
                    "Tagged CompanyCam photo",
                    "kitchen, demo",
                    "https://app.companycam.com/photos/8675309",
                ),
                (
                    "companycam_archive_project",
                    "Archived CompanyCam project",
                    "project",
                    "https://app.companycam.com/projects/94772883/photos",
                ),
            ]
        )
    ]
    block = format_receipts_block(fake_calls)

    # The rendered footer has two URL-keyed blocks (project + photo).
    assert block.count("https://app.companycam.com/projects/94772883/photos") == 1
    assert block.count("https://app.companycam.com/photos/8675309") == 1

    # Scan every visible token for a 6+ digit run: only the two URLs
    # are allowed to contain ids. Strip URLs then assert no digit runs.
    def _strip_urls(text: str) -> str:
        return re.sub(r"https://\S+", "", text)

    visible = _strip_urls(block)
    assert _RAW_ID_RE.search(visible) is None, (
        f"rendered footer leaked a raw CompanyCam id into user-visible text:\n{visible!r}"
    )

    # Block stays compact (grouping saves lines).
    assert len(block) < 500
