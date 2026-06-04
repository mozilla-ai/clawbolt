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


@pytest.mark.asyncio()
async def test_create_core_tools_excludes_disabled_factories() -> None:
    """create_core_tools should skip excluded factories."""
    user = User(id="999", user_id="test")
    ctx = ToolContext(user=user)

    all_core = await default_registry.create_core_tools(ctx)
    excluded = await default_registry.create_core_tools(ctx, excluded_factories={"workspace"})
    # Excluding workspace should remove read_file, write_file, etc.
    all_names = {t.name for t in all_core}
    excluded_names = {t.name for t in excluded}
    assert "read_file" in all_names
    assert "read_file" not in excluded_names


@pytest.mark.asyncio()
async def test_specialist_summaries_excludes_core_factories() -> None:
    """Core factories (workspace, profile, etc.) should not appear in specialist summaries."""
    user = User(id="999", user_id="test")
    ctx = ToolContext(user=user)

    summaries = await default_registry.get_available_specialist_summaries(ctx)
    for core_name in ("workspace", "profile", "memory", "messaging", "heartbeat"):
        assert core_name not in summaries, f"{core_name} should not be a specialist"

    # file/quickbooks/calendar are specialists (though they may be filtered by auth_check)
    assert "file" in default_registry.specialist_factory_names
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


def test_get_tool_config_includes_oauth_name(client: TestClient) -> None:
    """GET /api/user/tools surfaces the OAuth integration backing each tool.

    The frontend Settings UI uses ``oauth_name`` to render Connect /
    Disconnect buttons. Without this field, integrations whose factory
    name differs from the OAuth integration name (``file`` ->
    ``google_drive``, ``calendar`` -> ``google_calendar``) drop their
    Connect button silently, and integrations whose factory was never
    added to the frontend's hand-maintained map (e.g. ``gmail`` after the
    refactor in #1285) lose theirs too.
    """
    response = client.get("/api/user/tools")
    assert response.status_code == 200
    tools_by_name = {t["name"]: t for t in response.json()["tools"]}

    # All response entries declare the field (default empty string).
    for tool in tools_by_name.values():
        assert "oauth_name" in tool, f"{tool['name']} missing oauth_name field"

    # OAuth-backed factories report the integration name registered in
    # backend.app.services.oauth, including both the matching-name case
    # (``gmail``, ``quickbooks``, ``companycam``) and the renamed case
    # (``file`` -> ``google_drive``, ``calendar`` -> ``google_calendar``).
    assert tools_by_name["file"]["oauth_name"] == "google_drive"
    assert tools_by_name["calendar"]["oauth_name"] == "google_calendar"
    assert tools_by_name["quickbooks"]["oauth_name"] == "quickbooks"
    assert tools_by_name["companycam"]["oauth_name"] == "companycam"
    assert tools_by_name["gmail"]["oauth_name"] == "gmail"

    # Non-OAuth factories report empty string so the UI knows there is
    # nothing to connect.
    assert tools_by_name["workspace"]["oauth_name"] == ""
    assert tools_by_name["supplier_pricing"]["oauth_name"] == ""


def test_get_tool_config_includes_always_enabled(client: TestClient) -> None:
    """GET /api/user/tools surfaces ``always_enabled`` so the UI can hide the
    disable toggle for tools the backend refuses to disable.

    Mirrors ``ToolFactory.dashboard_always_enabled``. Decoupled from
    ``category`` so future internal-only categories cannot accidentally
    hide the toggle for always-on OAuth tools (Google Drive).
    """
    response = client.get("/api/user/tools")
    assert response.status_code == 200
    tools_by_name = {t["name"]: t for t in response.json()["tools"]}

    # All response entries declare the field (default False).
    for tool in tools_by_name.values():
        assert "always_enabled" in tool, f"{tool['name']} missing always_enabled"

    # Always-on factories report True. ``file`` (Google Drive) is the
    # canonical always-on OAuth factory; ``workspace`` is the canonical
    # always-on non-OAuth factory.
    assert tools_by_name["file"]["always_enabled"] is True
    assert tools_by_name["workspace"]["always_enabled"] is True

    # Specialist factories with toggles report False.
    assert tools_by_name["gmail"]["always_enabled"] is False
    assert tools_by_name["quickbooks"]["always_enabled"] is False


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


def test_visible_factories_declare_dashboard_description() -> None:
    """Every dashboard-visible factory must declare ``dashboard_description``.

    Regression for #1260 (dashboard half): dashboard metadata used to
    live in a hand-maintained ``_FACTORY_META`` dict in user_tools.py
    that had to be edited every time a new factory shipped. After moving
    the metadata onto ``ToolFactory``, this test pins the contract:
    a new factory that forgets to set ``dashboard_description`` will
    show up as an empty-string row in Settings, and this test will
    catch that at CI time rather than after deploy.

    Hidden backing factories (``_HIDDEN_CORE_FACTORIES``, currently empty)
    are filtered out of the dashboard and so are exempt.
    """
    from backend.app.agent.tools.integration_tools import _HIDDEN_CORE_FACTORIES

    for name in default_registry.factory_names:
        if name in _HIDDEN_CORE_FACTORIES:
            continue
        factory = default_registry.get_factory(name)
        assert factory is not None
        assert factory.dashboard_description, (
            f"Factory {name!r} is dashboard-visible but did not declare "
            "dashboard_description in its registry.register() call. Add "
            "it (and dashboard_group / dashboard_group_order if it is a "
            "specialist integration)."
        )


def test_specialist_dashboard_groups_are_consistent() -> None:
    """Specialist integrations rendered in the Settings UI must declare
    a non-empty ``dashboard_group`` and a positive ``dashboard_group_order``.

    Cross-checks the contract that core (always-on) factories and
    specialists are mutually exclusive in the Settings UI: core factories
    have ``dashboard_always_enabled=True`` and no group; specialists are
    grouped under e.g. "Integrations" with a sort order.
    """
    from backend.app.agent.tools.integration_tools import _HIDDEN_CORE_FACTORIES

    for name in default_registry.factory_names:
        if name in _HIDDEN_CORE_FACTORIES:
            continue
        factory = default_registry.get_factory(name)
        assert factory is not None
        if factory.dashboard_always_enabled:
            assert factory.dashboard_group == "", (
                f"Factory {name!r} is dashboard_always_enabled=True but "
                f"declared dashboard_group={factory.dashboard_group!r}. "
                "Always-on factories render in the core section, not a group."
            )
            assert factory.dashboard_group_order == 0, (
                f"Factory {name!r} is dashboard_always_enabled=True but "
                f"declared dashboard_group_order={factory.dashboard_group_order}. "
                "Always-on factories should leave the order at 0."
            )
        else:
            assert factory.dashboard_group, (
                f"Factory {name!r} is a dashboard specialist but did not "
                "declare dashboard_group. Set it (e.g. 'Integrations')."
            )
            assert factory.dashboard_group_order > 0, (
                f"Factory {name!r} is a dashboard specialist but did not "
                "declare a positive dashboard_group_order."
            )


# ---------------------------------------------------------------------------
# Sub-tool tests: registry layer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_create_core_tools_excludes_individual_tools() -> None:
    """excluded_tool_names filters individual tools after factory creation."""
    user = User(id="999", user_id="test")
    ctx = ToolContext(user=user)

    all_core = await default_registry.create_core_tools(ctx)
    excluded = await default_registry.create_core_tools(
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


def test_sub_tool_info_default_permission() -> None:
    """SubToolInfo carries correct default_permission for each tool."""
    ws_sub_tools = {st.name: st for st in default_registry.get_factory_sub_tools("workspace")}
    # read_file, write_file, edit_file are always; delete_file is ask
    assert ws_sub_tools["read_file"].default_permission == "always"
    assert ws_sub_tools["write_file"].default_permission == "always"
    assert ws_sub_tools["delete_file"].default_permission == "ask"

    # messaging tools default to always and are hidden from the Permissions UI
    msg_sub_tools = {st.name: st for st in default_registry.get_factory_sub_tools("messaging")}
    assert msg_sub_tools["send_media_reply"].default_permission == "always"
    assert msg_sub_tools["send_media_reply"].hidden_in_permissions is True


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

    # Each sub-tool should carry name, description, and permission_level.
    # ``enabled`` is gone: ``permission_level`` is the single source of
    # truth, and ``"never"`` is the off switch.
    for st in ws["sub_tools"]:
        assert "name" in st
        assert "description" in st
        assert "permission_level" in st
        assert st["permission_level"] in ("always", "ask", "never")


def test_put_tool_config_sets_sub_tool_permission_level(client: TestClient) -> None:
    """PUT /api/user/tools writes per-sub-tool permission_level overrides."""
    response = client.put(
        "/api/user/tools",
        json={
            "tools": [
                {
                    "name": "workspace",
                    "enabled": True,
                    "sub_tools": [
                        {"name": "write_file", "permission_level": "never"},
                        {"name": "delete_file", "permission_level": "ask"},
                    ],
                }
            ]
        },
    )
    assert response.status_code == 200
    tools_by_name = {t["name"]: t for t in response.json()["tools"]}
    sub_by_name = {st["name"]: st for st in tools_by_name["workspace"]["sub_tools"]}
    assert sub_by_name["write_file"]["permission_level"] == "never"
    assert sub_by_name["delete_file"]["permission_level"] == "ask"
    # Untouched sub-tools keep their default.
    assert sub_by_name["read_file"]["permission_level"] == "always"

    # Verify persistence via GET.
    get_resp = client.get("/api/user/tools")
    workspace_entries = [t for t in get_resp.json()["tools"] if t["name"] == "workspace"]
    assert workspace_entries, "workspace factory missing from response"
    sub_by_name = {st["name"]: st for st in workspace_entries[0]["sub_tools"]}
    assert sub_by_name["write_file"]["permission_level"] == "never"
    assert sub_by_name["delete_file"]["permission_level"] == "ask"


def test_put_tool_config_invalid_sub_tool_names_ignored(client: TestClient) -> None:
    """PUT /api/user/tools ignores unknown sub-tool names."""
    response = client.put(
        "/api/user/tools",
        json={
            "tools": [
                {
                    "name": "workspace",
                    "enabled": True,
                    "sub_tools": [
                        {"name": "nonexistent_tool", "permission_level": "never"},
                    ],
                }
            ]
        },
    )
    assert response.status_code == 200
    tools_by_name = {t["name"]: t for t in response.json()["tools"]}
    ws = tools_by_name["workspace"]

    # All sub-tools should remain at their default permission level
    # because the unknown name was filtered out.
    for st in ws["sub_tools"]:
        assert st["permission_level"] in ("always", "ask")


def test_put_tool_config_rejects_invalid_permission_level(client: TestClient) -> None:
    """PUT /api/user/tools rejects permission_level values outside the enum."""
    response = client.put(
        "/api/user/tools",
        json={
            "tools": [
                {
                    "name": "workspace",
                    "enabled": True,
                    "sub_tools": [
                        {"name": "write_file", "permission_level": "deny"},
                    ],
                }
            ]
        },
    )
    assert response.status_code == 400


def test_put_tool_config_can_re_enable_a_never_sub_tool(client: TestClient) -> None:
    """Setting a sub-tool back to ``always`` after ``never`` lifts the filter."""
    # First mark write_file as never.
    client.put(
        "/api/user/tools",
        json={
            "tools": [
                {
                    "name": "workspace",
                    "enabled": True,
                    "sub_tools": [
                        {"name": "write_file", "permission_level": "never"},
                    ],
                }
            ]
        },
    )
    # Then flip it back to always.
    response = client.put(
        "/api/user/tools",
        json={
            "tools": [
                {
                    "name": "workspace",
                    "enabled": True,
                    "sub_tools": [
                        {"name": "write_file", "permission_level": "always"},
                    ],
                }
            ]
        },
    )
    assert response.status_code == 200
    tools_by_name = {t["name"]: t for t in response.json()["tools"]}
    sub_by_name = {st["name"]: st for st in tools_by_name["workspace"]["sub_tools"]}
    assert sub_by_name["write_file"]["permission_level"] == "always"


@pytest.mark.asyncio()
async def test_never_permission_filters_sub_tool_from_agent_schema(
    test_user: UserData,
) -> None:
    """When a sub-tool's permission_level is ``never``, build_initial_turn_tools drops it.

    Regression for the original bug this refactor fixed: a sub-tool
    marked ``"never"`` in PERMISSIONS.json must not appear in the LLM
    schema. Without this, the agent could still see and call the tool
    even after the user said "never run this".
    """
    from backend.app.agent.approval import PermissionLevel, get_approval_store
    from backend.app.agent.tool_assembly import build_initial_turn_tools
    from backend.app.models import User

    await get_approval_store().set_permission(test_user.id, "write_file", PermissionLevel.NEVER)
    user = User(id=test_user.id, user_id=test_user.user_id)
    tools = await build_initial_turn_tools(user)
    names = {t.name for t in tools}
    assert "write_file" not in names
    # read_file (at default ALWAYS) is still present.
    assert "read_file" in names
