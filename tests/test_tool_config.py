"""Tests for the configurable tool registry (tool config API and store)."""

import pytest
from fastapi.testclient import TestClient

from backend.app.agent.file_store import (
    ToolConfigEntry,
    ToolConfigStore,
    UserData,
)
from backend.app.agent.tools.registry import (
    ToolContext,
    default_registry,
    ensure_tool_modules_imported,
)

ensure_tool_modules_imported()


# ---------------------------------------------------------------------------
# ToolConfigStore unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_tool_config_store_empty_on_first_load(
    test_user: UserData,
) -> None:
    """First load returns an empty list when no config file exists."""
    store = ToolConfigStore(test_user.id)
    entries = await store.load()
    assert entries == []


@pytest.mark.asyncio()
async def test_tool_config_store_save_and_load(
    test_user: UserData,
) -> None:
    """Saved entries can be loaded back."""
    store = ToolConfigStore(test_user.id)
    entries = [
        ToolConfigEntry(name="estimate", description="Estimates", category="domain", enabled=False),
        ToolConfigEntry(name="workspace", description="Files", category="core", enabled=True),
    ]
    saved = await store.save(entries)
    assert len(saved) == 2

    loaded = await store.load()
    assert len(loaded) == 2
    assert loaded[0].name == "estimate"
    assert loaded[0].enabled is False
    assert loaded[1].name == "workspace"
    assert loaded[1].enabled is True


@pytest.mark.asyncio()
async def test_tool_config_store_get_disabled_tool_names(
    test_user: UserData,
) -> None:
    """get_disabled_tool_names returns only disabled entries."""
    store = ToolConfigStore(test_user.id)
    entries = [
        ToolConfigEntry(name="estimate", category="domain", enabled=False),
        ToolConfigEntry(name="file", category="domain", enabled=True),
        ToolConfigEntry(name="checklist", category="domain", enabled=False),
    ]
    await store.save(entries)

    disabled = await store.get_disabled_tool_names()
    assert disabled == {"estimate", "checklist"}


# ---------------------------------------------------------------------------
# Registry exclusion tests
# ---------------------------------------------------------------------------


def test_create_core_tools_excludes_disabled_factories() -> None:
    """create_core_tools should skip excluded factories."""
    user = UserData(id=999, user_id="test")
    ctx = ToolContext(user=user)

    all_core = default_registry.create_core_tools(ctx)
    excluded = default_registry.create_core_tools(ctx, excluded_factories={"workspace"})
    # Excluding workspace should remove read_file, write_file, etc.
    all_names = {t.name for t in all_core}
    excluded_names = {t.name for t in excluded}
    assert "read_file" in all_names
    assert "read_file" not in excluded_names


def test_specialist_summaries_excludes_disabled_factories() -> None:
    """get_available_specialist_summaries should skip excluded factories."""
    user = UserData(id=999, user_id="test")
    ctx = ToolContext(user=user)

    all_summaries = default_registry.get_available_specialist_summaries(ctx)
    excluded_summaries = default_registry.get_available_specialist_summaries(
        ctx, excluded_factories={"estimate"}
    )
    assert "estimate" in all_summaries
    assert "estimate" not in excluded_summaries


# ---------------------------------------------------------------------------
# API endpoint tests
# ---------------------------------------------------------------------------


def test_get_tool_config(client: TestClient) -> None:
    """GET /api/user/tools returns all tools grouped by category."""
    response = client.get("/api/user/tools")
    assert response.status_code == 200
    data = response.json()
    assert "tools" in data
    tools = data["tools"]
    assert len(tools) > 0

    # All tools should have required fields
    for tool in tools:
        assert "name" in tool
        assert "description" in tool
        assert "category" in tool
        assert "domain_group" in tool
        assert "domain_group_order" in tool
        assert "enabled" in tool
        assert tool["category"] in ("core", "domain")

    # Core tools should all be enabled
    core_tools = [t for t in tools if t["category"] == "core"]
    assert len(core_tools) > 0
    for t in core_tools:
        assert t["enabled"] is True

    # Verify known factories are present
    names = {t["name"] for t in tools}
    assert "workspace" in names


def test_get_tool_config_domain_group(client: TestClient) -> None:
    """GET /api/user/tools returns domain_group for domain tools."""
    response = client.get("/api/user/tools")
    data = response.json()
    tools = data["tools"]

    # Domain tools should have a non-empty domain_group and positive order
    domain_tools = [t for t in tools if t["category"] == "domain"]
    for t in domain_tools:
        assert t["domain_group"] != "", f"{t['name']} missing domain_group"
        assert t["domain_group_order"] > 0, f"{t['name']} missing domain_group_order"

    # Core tools should have an empty domain_group and zero order
    core_tools = [t for t in tools if t["category"] == "core"]
    for t in core_tools:
        assert t["domain_group"] == "", f"{t['name']} should not have domain_group"
        assert t["domain_group_order"] == 0, f"{t['name']} should have zero order"


def test_put_tool_config_disable_domain_tool(client: TestClient) -> None:
    """PUT /api/user/tools can disable a domain tool."""
    response = client.put(
        "/api/user/tools",
        json={"tools": [{"name": "estimate", "enabled": False}]},
    )
    assert response.status_code == 200
    data = response.json()
    tools_by_name = {t["name"]: t for t in data["tools"]}
    assert tools_by_name["estimate"]["enabled"] is False

    # Verify it persists
    get_response = client.get("/api/user/tools")
    tools_by_name = {t["name"]: t for t in get_response.json()["tools"]}
    assert tools_by_name["estimate"]["enabled"] is False


def test_put_tool_config_cannot_disable_core_tool(client: TestClient) -> None:
    """PUT /api/user/tools silently ignores attempts to disable core tools."""
    response = client.put(
        "/api/user/tools",
        json={"tools": [{"name": "workspace", "enabled": False}]},
    )
    assert response.status_code == 200
    tools_by_name = {t["name"]: t for t in response.json()["tools"]}
    assert tools_by_name["workspace"]["enabled"] is True


def test_put_tool_config_reenable(client: TestClient) -> None:
    """PUT /api/user/tools can re-enable a previously disabled tool."""
    # Disable
    client.put(
        "/api/user/tools",
        json={"tools": [{"name": "estimate", "enabled": False}]},
    )
    # Re-enable
    response = client.put(
        "/api/user/tools",
        json={"tools": [{"name": "estimate", "enabled": True}]},
    )
    assert response.status_code == 200
    tools_by_name = {t["name"]: t for t in response.json()["tools"]}
    assert tools_by_name["estimate"]["enabled"] is True


def test_put_tool_config_empty_body(client: TestClient) -> None:
    """PUT /api/user/tools rejects empty tool list."""
    response = client.put("/api/user/tools", json={"tools": []})
    assert response.status_code == 400


def test_put_tool_config_unknown_tool_ignored(client: TestClient) -> None:
    """PUT /api/user/tools ignores unknown tool names without error."""
    response = client.put(
        "/api/user/tools",
        json={"tools": [{"name": "nonexistent_tool", "enabled": False}]},
    )
    assert response.status_code == 200
    # All tools should still be present and unchanged
    tools = response.json()["tools"]
    assert len(tools) > 0
