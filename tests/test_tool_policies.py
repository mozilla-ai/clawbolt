"""Tests verifying that each tool has the correct approval policy assigned."""

from unittest.mock import AsyncMock

from backend.app.agent.approval import PermissionLevel
from backend.app.agent.tools.base import Tool
from backend.app.agent.tools.heartbeat_tools import create_heartbeat_tools
from backend.app.agent.tools.messaging_tools import create_messaging_tools
from backend.app.agent.tools.names import ToolName
from backend.app.agent.tools.workspace_tools import create_workspace_tools


def _find_tool(tools: list[Tool], name: str) -> Tool:
    for t in tools:
        if t.name == name:
            return t
    raise ValueError(f"Tool {name!r} not found in {[t.name for t in tools]}")


class TestWorkspaceToolPolicies:
    def test_read_file_is_auto(self) -> None:
        tools = create_workspace_tools("test-user")
        tool = _find_tool(tools, ToolName.READ_FILE)
        assert tool.approval_policy is None  # No policy = AUTO

    def test_write_file_is_auto(self) -> None:
        tools = create_workspace_tools("test-user")
        tool = _find_tool(tools, ToolName.WRITE_FILE)
        assert tool.approval_policy is not None
        assert tool.approval_policy.default_level == PermissionLevel.AUTO

    def test_edit_file_is_auto(self) -> None:
        tools = create_workspace_tools("test-user")
        tool = _find_tool(tools, ToolName.EDIT_FILE)
        assert tool.approval_policy is not None
        assert tool.approval_policy.default_level == PermissionLevel.AUTO

    def test_delete_file_is_ask(self) -> None:
        tools = create_workspace_tools("test-user")
        tool = _find_tool(tools, ToolName.DELETE_FILE)
        assert tool.approval_policy is not None
        assert tool.approval_policy.default_level == PermissionLevel.ASK

    def test_write_file_description_builder(self) -> None:
        tools = create_workspace_tools("test-user")
        tool = _find_tool(tools, ToolName.WRITE_FILE)
        assert tool.approval_policy is not None
        assert tool.approval_policy.description_builder is not None
        desc = tool.approval_policy.description_builder({"path": "USER.md", "content": "x"})
        assert "USER.md" in desc


class TestMessagingToolPolicies:
    def test_send_reply_is_ask(self) -> None:
        tools = create_messaging_tools(AsyncMock(), "telegram", "123")
        tool = _find_tool(tools, ToolName.SEND_REPLY)
        assert tool.approval_policy is not None
        assert tool.approval_policy.default_level == PermissionLevel.ASK

    def test_send_media_reply_is_ask(self) -> None:
        tools = create_messaging_tools(AsyncMock(), "telegram", "123")
        tool = _find_tool(tools, ToolName.SEND_MEDIA_REPLY)
        assert tool.approval_policy is not None
        assert tool.approval_policy.default_level == PermissionLevel.ASK

    def test_send_media_description_builder(self) -> None:
        tools = create_messaging_tools(AsyncMock(), "telegram", "123")
        tool = _find_tool(tools, ToolName.SEND_MEDIA_REPLY)
        assert tool.approval_policy is not None
        assert tool.approval_policy.description_builder is not None
        desc = tool.approval_policy.description_builder(
            {"message": "hi", "media_url": "https://example.com/file.pdf"}
        )
        assert desc == "Send a file attachment"


class TestHeartbeatToolPolicies:
    def test_get_heartbeat_is_auto(self) -> None:
        tools = create_heartbeat_tools("test-user")
        tool = _find_tool(tools, ToolName.GET_HEARTBEAT)
        assert tool.approval_policy is None  # No policy = AUTO

    def test_update_heartbeat_is_auto(self) -> None:
        tools = create_heartbeat_tools("test-user")
        tool = _find_tool(tools, ToolName.UPDATE_HEARTBEAT)
        assert tool.approval_policy is not None
        assert tool.approval_policy.default_level == PermissionLevel.AUTO


class TestWorkspaceResourceExtractors:
    def test_write_file_extracts_path(self) -> None:
        tools = create_workspace_tools("test-user")
        tool = _find_tool(tools, ToolName.WRITE_FILE)
        assert tool.approval_policy is not None
        assert tool.approval_policy.resource_extractor is not None
        resource = tool.approval_policy.resource_extractor({"path": "USER.md", "content": "x"})
        assert resource == "USER.md"

    def test_edit_file_extracts_path(self) -> None:
        tools = create_workspace_tools("test-user")
        tool = _find_tool(tools, ToolName.EDIT_FILE)
        assert tool.approval_policy is not None
        assert tool.approval_policy.resource_extractor is not None
        resource = tool.approval_policy.resource_extractor(
            {"path": "SOUL.md", "old_text": "a", "new_text": "b"}
        )
        assert resource == "SOUL.md"

    def test_delete_file_extracts_path(self) -> None:
        tools = create_workspace_tools("test-user")
        tool = _find_tool(tools, ToolName.DELETE_FILE)
        assert tool.approval_policy is not None
        assert tool.approval_policy.resource_extractor is not None
        resource = tool.approval_policy.resource_extractor({"path": "BOOTSTRAP.md"})
        assert resource == "BOOTSTRAP.md"

    def test_read_file_has_no_extractor(self) -> None:
        tools = create_workspace_tools("test-user")
        tool = _find_tool(tools, ToolName.READ_FILE)
        assert tool.approval_policy is None


class TestQuickBooksResourceExtractors:
    def test_qb_query_extracts_entity_from_query(self) -> None:
        from backend.app.agent.tools.quickbooks_tools import _extract_query_entity

        assert _extract_query_entity({"query": "SELECT * FROM Invoice"}) == "Invoice"
        assert _extract_query_entity({"query": "select Id from Customer"}) == "Customer"
        assert _extract_query_entity({"query": "bad query"}) is None

    def test_qb_create_extracts_entity_type(self) -> None:
        from backend.app.agent.tools.quickbooks_tools import _extract_entity_type

        assert _extract_entity_type({"entity_type": "Invoice", "data": {}}) == "Invoice"
        assert _extract_entity_type({"entity_type": "Customer", "data": {}}) == "Customer"
        assert _extract_entity_type({}) is None

    def test_qb_send_extracts_email(self) -> None:
        from backend.app.agent.tools.quickbooks_tools import _extract_send_email

        assert _extract_send_email({"email": "bob@example.com"}) == "bob@example.com"
        assert _extract_send_email({}) is None
