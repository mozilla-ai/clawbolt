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
from backend.app.models import User

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
        ToolConfigEntry(name="heartbeat", category="domain", enabled=False),
    ]
    await store.save(entries)

    disabled = await store.get_disabled_tool_names()
    assert disabled == {"estimate", "heartbeat"}


# ---------------------------------------------------------------------------
# Registry exclusion tests
# ---------------------------------------------------------------------------


def test_create_core_tools_excludes_disabled_factories() -> None:
    """create_core_tools should skip excluded factories."""
    user = User(id="999", user_id="test")
    ctx = ToolContext(user=user)

    all_core = default_registry.create_core_tools(ctx)
    excluded = default_registry.create_core_tools(ctx, excluded_factories={"workspace"})
    # Excluding workspace should remove read_file, write_file, etc.
    all_names = {t.name for t in all_core}
    excluded_names = {t.name for t in excluded}
    assert "read_file" in all_names
    assert "read_file" not in excluded_names


def test_specialist_summaries_excludes_core_factories() -> None:
    """Core factories (including file and heartbeat) should not appear in specialist summaries."""
    user = User(id="999", user_id="test")
    ctx = ToolContext(user=user)

    summaries = default_registry.get_available_specialist_summaries(ctx)
    for core_name in ("workspace", "profile", "memory", "messaging", "file", "heartbeat"):
        assert core_name not in summaries, f"{core_name} should not be a specialist"

    # quickbooks and calendar are specialists (though they may be filtered by auth_check)
    assert "quickbooks" in default_registry.specialist_factory_names
    assert "calendar" in default_registry.specialist_factory_names


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
        json={"tools": [{"name": "quickbooks", "enabled": False}]},
    )
    assert response.status_code == 200
    data = response.json()
    tools_by_name = {t["name"]: t for t in data["tools"]}
    assert tools_by_name["quickbooks"]["enabled"] is False

    # Verify it persists
    get_response = client.get("/api/user/tools")
    tools_by_name = {t["name"]: t for t in get_response.json()["tools"]}
    assert tools_by_name["quickbooks"]["enabled"] is False


def test_put_tool_config_cannot_disable_core_tool(client: TestClient) -> None:
    """PUT /api/user/tools silently ignores attempts to disable core tools."""
    # Test original core tools and newly promoted core tools
    for tool_name in ("workspace", "heartbeat", "file"):
        response = client.put(
            "/api/user/tools",
            json={"tools": [{"name": tool_name, "enabled": False}]},
        )
        assert response.status_code == 200
        tools_by_name = {t["name"]: t for t in response.json()["tools"]}
        assert tools_by_name[tool_name]["enabled"] is True, f"{tool_name} should not be disableable"


def test_put_tool_config_reenable(client: TestClient) -> None:
    """PUT /api/user/tools can re-enable a previously disabled tool."""
    # Disable
    client.put(
        "/api/user/tools",
        json={"tools": [{"name": "quickbooks", "enabled": False}]},
    )
    # Re-enable
    response = client.put(
        "/api/user/tools",
        json={"tools": [{"name": "quickbooks", "enabled": True}]},
    )
    assert response.status_code == 200
    tools_by_name = {t["name"]: t for t in response.json()["tools"]}
    assert tools_by_name["quickbooks"]["enabled"] is True


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


# ---------------------------------------------------------------------------
# Sub-tool tests: store layer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_tool_config_store_disabled_sub_tools_round_trip(
    test_user: UserData,
) -> None:
    """disabled_sub_tools survive a save/load cycle."""
    store = ToolConfigStore(test_user.id)
    entries = [
        ToolConfigEntry(
            name="workspace",
            description="Files",
            category="core",
            enabled=True,
            disabled_sub_tools=["write_file", "delete_file"],
        ),
    ]
    await store.save(entries)

    loaded = await store.load()
    assert len(loaded) == 1
    assert loaded[0].disabled_sub_tools == ["write_file", "delete_file"]


@pytest.mark.asyncio()
async def test_tool_config_store_get_disabled_sub_tool_names(
    test_user: UserData,
) -> None:
    """get_disabled_sub_tool_names unions disabled sub-tools across all groups."""
    store = ToolConfigStore(test_user.id)
    entries = [
        ToolConfigEntry(
            name="workspace",
            category="core",
            enabled=True,
            disabled_sub_tools=["write_file", "delete_file"],
        ),
        ToolConfigEntry(
            name="heartbeat",
            category="domain",
            enabled=True,
            disabled_sub_tools=["update_heartbeat"],
        ),
        ToolConfigEntry(name="file", category="domain", enabled=True),
    ]
    await store.save(entries)

    disabled = await store.get_disabled_sub_tool_names()
    assert disabled == {"write_file", "delete_file", "update_heartbeat"}


# ---------------------------------------------------------------------------
# Sub-tool tests: registry layer
# ---------------------------------------------------------------------------


def test_create_core_tools_excludes_individual_tools() -> None:
    """excluded_tool_names filters individual tools after factory creation."""
    user = User(id="999", user_id="test")
    ctx = ToolContext(user=user)

    all_core = default_registry.create_core_tools(ctx)
    excluded = default_registry.create_core_tools(
        ctx, excluded_tool_names={"write_file", "delete_file"}
    )

    all_names = {t.name for t in all_core}
    excluded_names = {t.name for t in excluded}

    # write_file and delete_file should be removed
    assert "write_file" in all_names
    assert "delete_file" in all_names
    assert "write_file" not in excluded_names
    assert "delete_file" not in excluded_names
    # read_file should still be present
    assert "read_file" in excluded_names


def test_get_factory_sub_tools_returns_metadata() -> None:
    """get_factory_sub_tools returns SubToolInfo for registered factories."""
    sub_tools = default_registry.get_factory_sub_tools("workspace")
    names = {st.name for st in sub_tools}
    assert "read_file" in names
    assert "write_file" in names


def test_get_factory_sub_tools_unknown_factory() -> None:
    """get_factory_sub_tools returns empty list for unknown factory names."""
    sub_tools = default_registry.get_factory_sub_tools("nonexistent")
    assert sub_tools == []


# ---------------------------------------------------------------------------
# Sub-tool tests: API layer
# ---------------------------------------------------------------------------


def test_get_tool_config_includes_sub_tools(client: TestClient) -> None:
    """GET /api/user/tools returns sub_tools array for each tool."""
    response = client.get("/api/user/tools")
    assert response.status_code == 200
    tools_by_name = {t["name"]: t for t in response.json()["tools"]}

    # workspace should have sub_tools
    ws = tools_by_name["workspace"]
    assert "sub_tools" in ws
    sub_names = {st["name"] for st in ws["sub_tools"]}
    assert "read_file" in sub_names
    assert "write_file" in sub_names

    # Each sub-tool should have name, description, and enabled
    for st in ws["sub_tools"]:
        assert "name" in st
        assert "description" in st
        assert "enabled" in st
        assert st["enabled"] is True  # all enabled by default


def test_put_tool_config_disable_sub_tools(client: TestClient) -> None:
    """PUT /api/user/tools can disable individual sub-tools."""
    response = client.put(
        "/api/user/tools",
        json={
            "tools": [
                {
                    "name": "workspace",
                    "enabled": True,
                    "disabled_sub_tools": ["write_file", "delete_file"],
                }
            ]
        },
    )
    assert response.status_code == 200
    tools_by_name = {t["name"]: t for t in response.json()["tools"]}
    ws = tools_by_name["workspace"]

    sub_by_name = {st["name"]: st for st in ws["sub_tools"]}
    assert sub_by_name["read_file"]["enabled"] is True
    assert sub_by_name["write_file"]["enabled"] is False
    assert sub_by_name["delete_file"]["enabled"] is False

    # Verify persistence via GET
    get_resp = client.get("/api/user/tools")
    tools_by_name = {t["name"]: t for t in get_resp.json()["tools"]}
    sub_by_name = {st["name"]: st for st in tools_by_name["workspace"]["sub_tools"]}
    assert sub_by_name["write_file"]["enabled"] is False
    assert sub_by_name["delete_file"]["enabled"] is False


def test_put_tool_config_invalid_sub_tool_names_ignored(client: TestClient) -> None:
    """PUT /api/user/tools ignores invalid sub-tool names."""
    response = client.put(
        "/api/user/tools",
        json={
            "tools": [
                {
                    "name": "workspace",
                    "enabled": True,
                    "disabled_sub_tools": ["nonexistent_tool"],
                }
            ]
        },
    )
    assert response.status_code == 200
    tools_by_name = {t["name"]: t for t in response.json()["tools"]}
    ws = tools_by_name["workspace"]

    # All sub-tools should still be enabled since the invalid name was filtered out
    for st in ws["sub_tools"]:
        assert st["enabled"] is True


def test_put_tool_config_clear_disabled_sub_tools(client: TestClient) -> None:
    """Sending empty disabled_sub_tools list clears previous disablement."""
    # First disable some sub-tools
    client.put(
        "/api/user/tools",
        json={
            "tools": [
                {
                    "name": "workspace",
                    "enabled": True,
                    "disabled_sub_tools": ["write_file"],
                }
            ]
        },
    )
    # Then clear by sending empty list
    response = client.put(
        "/api/user/tools",
        json={
            "tools": [
                {
                    "name": "workspace",
                    "enabled": True,
                    "disabled_sub_tools": [],
                }
            ]
        },
    )
    assert response.status_code == 200
    tools_by_name = {t["name"]: t for t in response.json()["tools"]}
    for st in tools_by_name["workspace"]["sub_tools"]:
        assert st["enabled"] is True
