"""Tests for ServiceNow FSM tool functions and factory."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from backend.app.agent.tools.servicenow_time import build_time_tools
from backend.app.agent.tools.servicenow_work_orders import build_work_order_tools
from backend.app.services.servicenow import ServiceNowService
from backend.app.services.servicenow_models import (
    SNDisplayValue,
    TimeCard,
    WorkOrder,
    WorkOrderTask,
)


def _make_service() -> ServiceNowService:
    return ServiceNowService(
        access_token="test-token",
        instance_url="https://test.service-now.com",
        sys_user_id="user123",
    )


def _make_work_order(**kwargs: object) -> WorkOrder:
    defaults = {
        "sys_id": "wo1",
        "number": "WO0001",
        "short_description": "HVAC Install",
        "state": SNDisplayValue(value="1", display_value="Assigned"),
        "priority": SNDisplayValue(value="2", display_value="High"),
        "assigned_to": SNDisplayValue(value="user123", display_value="John"),
        "location": SNDisplayValue(),
    }
    defaults.update(kwargs)
    return WorkOrder(**defaults)


def _make_task(**kwargs: object) -> WorkOrderTask:
    defaults = {
        "sys_id": "task1",
        "number": "WMT0001",
        "short_description": "Remove old unit",
        "state": SNDisplayValue(value="3", display_value="Work In Progress"),
        "assigned_to": SNDisplayValue(value="user123", display_value="John"),
        "work_order": SNDisplayValue(value="wo1", display_value="WO0001"),
    }
    defaults.update(kwargs)
    return WorkOrderTask(**defaults)


class TestWorkOrderTools:
    def test_tool_count(self) -> None:
        service = _make_service()
        tools = build_work_order_tools(service)
        assert len(tools) == 7  # list, get, list_tasks, update, 2 notes, search

    def test_tool_names(self) -> None:
        service = _make_service()
        tools = build_work_order_tools(service)
        names = {t.name for t in tools}
        assert "servicenow_list_work_orders" in names
        assert "servicenow_get_work_order" in names
        assert "servicenow_list_tasks" in names
        assert "servicenow_update_task" in names
        assert "servicenow_add_work_order_note" in names
        assert "servicenow_add_task_note" in names
        assert "servicenow_search" in names

    def test_mutating_tools_have_approval_policy(self) -> None:
        service = _make_service()
        tools = build_work_order_tools(service)
        mutating = [
            "servicenow_update_task",
            "servicenow_add_work_order_note",
            "servicenow_add_task_note",
        ]
        for tool in tools:
            if tool.name in mutating:
                assert tool.approval_policy is not None, f"{tool.name} missing approval_policy"

    @pytest.mark.asyncio()
    async def test_list_work_orders_empty(self) -> None:
        service = _make_service()
        service.list_work_orders = AsyncMock(return_value=[])
        tools = build_work_order_tools(service)
        list_tool = next(t for t in tools if t.name == "servicenow_list_work_orders")
        result = await list_tool.function()
        assert "No work orders found" in result.content
        assert not result.is_error

    @pytest.mark.asyncio()
    async def test_list_work_orders_formats_results(self) -> None:
        service = _make_service()
        service.list_work_orders = AsyncMock(return_value=[_make_work_order()])
        tools = build_work_order_tools(service)
        list_tool = next(t for t in tools if t.name == "servicenow_list_work_orders")
        result = await list_tool.function()
        assert "WO0001" in result.content
        assert "HVAC Install" in result.content

    @pytest.mark.asyncio()
    async def test_list_work_orders_error(self) -> None:
        service = _make_service()
        service.list_work_orders = AsyncMock(side_effect=Exception("Connection refused"))
        tools = build_work_order_tools(service)
        list_tool = next(t for t in tools if t.name == "servicenow_list_work_orders")
        result = await list_tool.function()
        assert result.is_error
        assert result.error_kind is not None

    @pytest.mark.asyncio()
    async def test_update_task_returns_receipt(self) -> None:
        service = _make_service()
        service.update_task = AsyncMock(return_value=_make_task())
        tools = build_work_order_tools(service)
        update_tool = next(t for t in tools if t.name == "servicenow_update_task")
        result = await update_tool.function(sys_id="task1", state="Work In Progress")
        assert result.receipt is not None
        assert result.receipt.action == "Updated task status"
        assert "WMT0001" in result.receipt.target
        assert "test.service-now.com" in result.receipt.url

    @pytest.mark.asyncio()
    async def test_add_work_order_note_returns_receipt(self) -> None:
        service = _make_service()
        service.add_work_order_note = AsyncMock(return_value=_make_work_order())
        tools = build_work_order_tools(service)
        note_tool = next(t for t in tools if t.name == "servicenow_add_work_order_note")
        result = await note_tool.function(sys_id="wo1", note="Test note")
        assert result.receipt is not None
        assert result.receipt.action == "Added work note"

    @pytest.mark.asyncio()
    async def test_add_task_note_returns_receipt(self) -> None:
        service = _make_service()
        service.add_task_note = AsyncMock(return_value=_make_task())
        tools = build_work_order_tools(service)
        note_tool = next(t for t in tools if t.name == "servicenow_add_task_note")
        result = await note_tool.function(sys_id="task1", note="Test note")
        assert result.receipt is not None
        assert result.receipt.action == "Added work note"

    @pytest.mark.asyncio()
    async def test_search_empty_results(self) -> None:
        service = _make_service()
        service.search_work_orders = AsyncMock(return_value=[])
        tools = build_work_order_tools(service)
        search_tool = next(t for t in tools if t.name == "servicenow_search")
        result = await search_tool.function(query="nonexistent")
        assert "No work orders found" in result.content


class TestTimeTools:
    def test_tool_count(self) -> None:
        service = _make_service()
        tools = build_time_tools(service)
        assert len(tools) == 1

    def test_has_approval_policy(self) -> None:
        service = _make_service()
        tools = build_time_tools(service)
        assert tools[0].approval_policy is not None

    @pytest.mark.asyncio()
    async def test_log_time_returns_receipt(self) -> None:
        service = _make_service()
        service.create_time_card = AsyncMock(
            return_value=TimeCard(
                sys_id="tc1",
                task=SNDisplayValue(value="task1", display_value="WMT0001"),
                user=SNDisplayValue(value="u1", display_value="John"),
                total="2.5",
                date="2026-04-23",
                state="Submitted",
                category="labor",
            )
        )
        tools = build_time_tools(service)
        result = await tools[0].function(
            task_id="task1",
            hours=2.5,
            date="2026-04-23",
            category="labor",
        )
        assert result.receipt is not None
        assert result.receipt.action == "Logged time"
        assert "2.5h" in result.receipt.target

    @pytest.mark.asyncio()
    async def test_log_time_error(self) -> None:
        service = _make_service()
        service.create_time_card = AsyncMock(side_effect=Exception("timeout"))
        tools = build_time_tools(service)
        result = await tools[0].function(
            task_id="task1",
            hours=1.0,
            date="2026-04-23",
        )
        assert result.is_error


class TestTotalToolCount:
    def test_all_tools_combined(self) -> None:
        """Factory should produce exactly 8 tools total."""
        service = _make_service()
        all_tools = build_work_order_tools(service) + build_time_tools(service)
        assert len(all_tools) == 8
