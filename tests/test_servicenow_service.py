"""Tests for the ServiceNow Table API service layer."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from backend.app.services.servicenow import ServiceNowService, validate_instance_url
from backend.app.services.servicenow_models import SNDisplayValue, WorkOrder

# -- Instance URL validation ---------------------------------------------------


class TestValidateInstanceUrl:
    def test_valid_service_now_url(self) -> None:
        assert validate_instance_url("https://mycompany.service-now.com") == (
            "https://mycompany.service-now.com"
        )

    def test_valid_servicenow_url(self) -> None:
        assert validate_instance_url("https://mycompany.servicenow.com") == (
            "https://mycompany.servicenow.com"
        )

    def test_strips_trailing_slash(self) -> None:
        assert validate_instance_url("https://mycompany.service-now.com/") == (
            "https://mycompany.service-now.com"
        )

    def test_rejects_non_servicenow_url(self) -> None:
        with pytest.raises(ValueError, match="Invalid ServiceNow instance URL"):
            validate_instance_url("https://evil.com")

    def test_rejects_http_url(self) -> None:
        with pytest.raises(ValueError, match="Invalid ServiceNow instance URL"):
            validate_instance_url("http://mycompany.service-now.com")

    def test_rejects_empty(self) -> None:
        with pytest.raises(ValueError, match="Invalid ServiceNow instance URL"):
            validate_instance_url("")

    def test_rejects_path_traversal(self) -> None:
        with pytest.raises(ValueError, match="Invalid ServiceNow instance URL"):
            validate_instance_url("https://mycompany.service-now.com/evil")

    def test_case_insensitive(self) -> None:
        result = validate_instance_url("https://MyCompany.Service-Now.COM")
        assert result == "https://MyCompany.Service-Now.COM"


# -- Constructor ---------------------------------------------------------------


class TestServiceConstructor:
    def test_requires_access_token(self) -> None:
        with pytest.raises(ValueError, match="access token"):
            ServiceNowService(access_token="", instance_url="https://x.service-now.com")

    def test_validates_instance_url(self) -> None:
        with pytest.raises(ValueError, match="Invalid ServiceNow"):
            ServiceNowService(access_token="tok", instance_url="https://evil.com")

    def test_stores_sys_user_id(self) -> None:
        svc = ServiceNowService(
            access_token="tok",
            instance_url="https://x.service-now.com",
            sys_user_id="abc123",
        )
        assert svc.sys_user_id == "abc123"


# -- Helpers -------------------------------------------------------------------


def _mock_response(data: dict | list, status: int = 200) -> httpx.Response:
    """Build a fake httpx.Response with JSON body."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.json.return_value = {"result": data}
    resp.raise_for_status = MagicMock()
    if status >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            message="error",
            request=MagicMock(),
            response=resp,
        )
    return resp


def _make_service(sys_user_id: str = "") -> ServiceNowService:
    return ServiceNowService(
        access_token="test-token",
        instance_url="https://test.service-now.com",
        sys_user_id=sys_user_id,
    )


def _patch_httpx() -> AsyncMock:
    """Return a mock that replaces httpx.AsyncClient context manager."""
    client = AsyncMock()
    mock_cls = patch("backend.app.services.servicenow.httpx.AsyncClient")
    mock = mock_cls.start()
    mock.return_value.__aenter__ = AsyncMock(return_value=client)
    mock.return_value.__aexit__ = AsyncMock(return_value=False)
    return client


# -- API methods ---------------------------------------------------------------


class TestListWorkOrders:
    @pytest.mark.asyncio()
    async def test_returns_work_orders(self) -> None:
        service = _make_service(sys_user_id="user123")
        client = _patch_httpx()
        client.get = AsyncMock(
            return_value=_mock_response(
                [
                    {
                        "sys_id": "wo1",
                        "number": "WO0001",
                        "short_description": "HVAC Install",
                        "state": {"value": "1", "display_value": "Assigned", "link": ""},
                        "priority": {"value": "2", "display_value": "High", "link": ""},
                        "assigned_to": {
                            "value": "user123",
                            "display_value": "John",
                            "link": "",
                        },
                        "location": {"value": "", "display_value": "", "link": ""},
                    }
                ]
            )
        )

        try:
            orders = await service.list_work_orders()
        finally:
            patch.stopall()

        assert len(orders) == 1
        assert orders[0].number == "WO0001"
        assert orders[0].state.display_value == "Assigned"

    @pytest.mark.asyncio()
    async def test_defaults_assigned_to_sys_user_id(self) -> None:
        service = _make_service(sys_user_id="myuser")
        client = _patch_httpx()
        client.get = AsyncMock(return_value=_mock_response([]))

        try:
            await service.list_work_orders()
        finally:
            patch.stopall()

        call_kwargs = client.get.call_args
        params = call_kwargs.kwargs.get("params", {})
        assert "assigned_to=myuser" in params.get("sysparm_query", "")

    @pytest.mark.asyncio()
    async def test_caps_limit_at_50(self) -> None:
        service = _make_service()
        client = _patch_httpx()
        client.get = AsyncMock(return_value=_mock_response([]))

        try:
            await service.list_work_orders(limit=100)
        finally:
            patch.stopall()

        call_kwargs = client.get.call_args
        params = call_kwargs.kwargs.get("params", {})
        assert params["sysparm_limit"] == "50"


class TestSearchWorkOrders:
    @pytest.mark.asyncio()
    async def test_builds_server_side_query(self) -> None:
        service = _make_service()
        client = _patch_httpx()
        client.get = AsyncMock(return_value=_mock_response([]))

        try:
            await service.search_work_orders("HVAC")
        finally:
            patch.stopall()

        call_kwargs = client.get.call_args
        params = call_kwargs.kwargs.get("params", {})
        query = params.get("sysparm_query", "")
        assert "short_descriptionLIKEHVAC" in query
        assert "numberLIKEHVAC" in query

    @pytest.mark.asyncio()
    async def test_strips_injection_characters(self) -> None:
        service = _make_service()
        client = _patch_httpx()
        client.get = AsyncMock(return_value=_mock_response([]))

        try:
            await service.search_work_orders("test^NQstate=1")
        finally:
            patch.stopall()

        call_kwargs = client.get.call_args
        params = call_kwargs.kwargs.get("params", {})
        query = params.get("sysparm_query", "")
        # The ^ character should be stripped
        assert "^NQ" not in query

    @pytest.mark.asyncio()
    async def test_empty_query_returns_empty(self) -> None:
        service = _make_service()
        result = await service.search_work_orders("")
        assert result == []


class TestUpdateTask:
    @pytest.mark.asyncio()
    async def test_sends_patch_with_state(self) -> None:
        service = _make_service()
        client = _patch_httpx()
        client.patch = AsyncMock(
            return_value=_mock_response(
                {
                    "sys_id": "task1",
                    "number": "WMT0001",
                    "short_description": "Remove old unit",
                    "state": {"value": "3", "display_value": "Work In Progress", "link": ""},
                    "assigned_to": {"value": "", "display_value": "", "link": ""},
                    "work_order": {"value": "", "display_value": "", "link": ""},
                }
            )
        )

        try:
            task = await service.update_task("task1", state="Work In Progress")
        finally:
            patch.stopall()

        assert task.number == "WMT0001"
        call_kwargs = client.patch.call_args
        assert call_kwargs.kwargs["json"]["state"] == "Work In Progress"

    @pytest.mark.asyncio()
    async def test_requires_state_or_notes(self) -> None:
        service = _make_service()
        with pytest.raises(ValueError, match="At least one"):
            await service.update_task("task1")


class TestResolveCurrentUser:
    @pytest.mark.asyncio()
    async def test_returns_sys_id(self) -> None:
        service = _make_service()
        client = _patch_httpx()
        # Simulate /api/now/myuser returning a user record
        client.get = AsyncMock(
            return_value=_mock_response({"sys_id": "resolved123", "user_name": "jdoe"})
        )

        try:
            result = await service.resolve_current_user()
        finally:
            patch.stopall()

        assert result == "resolved123"

    @pytest.mark.asyncio()
    async def test_returns_empty_on_no_result(self) -> None:
        service = _make_service()
        client = _patch_httpx()
        client.get = AsyncMock(return_value=_mock_response(None))

        try:
            result = await service.resolve_current_user()
        finally:
            patch.stopall()

        assert result == ""


class TestCreateTimeCard:
    @pytest.mark.asyncio()
    async def test_posts_time_card(self) -> None:
        service = _make_service()
        client = _patch_httpx()
        client.post = AsyncMock(
            return_value=_mock_response(
                {
                    "sys_id": "tc1",
                    "task": {"value": "task1", "display_value": "WMT0001", "link": ""},
                    "user": {"value": "u1", "display_value": "John", "link": ""},
                    "total": "2.5",
                    "date": "2026-04-23",
                    "state": "Submitted",
                    "category": "labor",
                }
            )
        )

        try:
            card = await service.create_time_card(
                task_id="task1",
                hours=2.5,
                date="2026-04-23",
                category="labor",
            )
        finally:
            patch.stopall()

        assert card.sys_id == "tc1"
        assert card.total == "2.5"


# -- Model tests ---------------------------------------------------------------


class TestSNDisplayValue:
    def test_str_returns_display_value(self) -> None:
        dv = SNDisplayValue(value="123", display_value="High")
        assert str(dv) == "High"

    def test_str_falls_back_to_value(self) -> None:
        dv = SNDisplayValue(value="123", display_value="")
        assert str(dv) == "123"

    def test_parses_from_dict(self) -> None:
        dv = SNDisplayValue.model_validate(
            {"value": "abc", "display_value": "Label", "link": "https://..."}
        )
        assert dv.value == "abc"
        assert dv.display_value == "Label"


class TestWorkOrderModel:
    def test_extra_fields_allowed(self) -> None:
        """ServiceNow may return fields not in our model."""
        wo = WorkOrder.model_validate(
            {
                "sys_id": "x",
                "number": "WO0001",
                "unknown_field": "ignored",
            }
        )
        assert wo.sys_id == "x"
