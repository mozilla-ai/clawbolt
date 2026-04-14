"""Tests for media_staging cache and cross-turn upload_to_storage recovery."""

from __future__ import annotations

import time
from collections.abc import Generator
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel, Field

from backend.app.agent import media_staging
from backend.app.agent.approval import (
    ApprovalDecision,
    ApprovalPolicy,
    PermissionLevel,
    get_approval_gate,
    get_approval_store,
)
from backend.app.agent.core import ClawboltAgent
from backend.app.agent.llm_parsing import ParsedToolCall
from backend.app.agent.messages import ToolCallRequest
from backend.app.agent.tools.base import Tool, ToolResult
from backend.app.agent.tools.file_tools import _file_factory, auto_save_media, create_file_tools
from backend.app.agent.tools.registry import ToolContext
from backend.app.media.download import DownloadedMedia
from backend.app.models import User
from tests.mocks.storage import MockStorageBackend


@pytest.fixture(autouse=True)
def _clear_staging_between_tests(test_user: User) -> Generator[None]:
    media_staging.clear_user(test_user.id)
    yield
    media_staging.clear_user(test_user.id)


def test_stage_and_retrieve(test_user: User) -> None:
    media_staging.stage(test_user.id, "bb_abc", b"bytes", "image/jpeg")
    result = media_staging.get_all_for_user(test_user.id)
    assert result == {"bb_abc": b"bytes"}


def test_stage_ignores_empty_url_or_content(test_user: User) -> None:
    media_staging.stage(test_user.id, "", b"bytes", "image/jpeg")
    media_staging.stage(test_user.id, "bb_empty", b"", "image/jpeg")
    assert media_staging.get_all_for_user(test_user.id) == {}


def test_evict_removes_entry(test_user: User) -> None:
    media_staging.stage(test_user.id, "bb_abc", b"bytes", "image/jpeg")
    media_staging.evict(test_user.id, "bb_abc")
    assert media_staging.get_all_for_user(test_user.id) == {}


def test_expired_entries_are_purged(test_user: User, monkeypatch: pytest.MonkeyPatch) -> None:
    media_staging.stage(test_user.id, "bb_old", b"bytes", "image/jpeg")
    # Fast-forward past the TTL.
    real_monotonic = time.monotonic
    future = real_monotonic() + media_staging.STAGING_TTL_SECONDS + 1
    monkeypatch.setattr(media_staging.time, "monotonic", lambda: future)
    assert media_staging.get_all_for_user(test_user.id) == {}


def test_isolation_between_users() -> None:
    media_staging.stage("user-a", "bb_abc", b"bytes-a", "image/jpeg")
    media_staging.stage("user-b", "bb_abc", b"bytes-b", "image/jpeg")
    assert media_staging.get_all_for_user("user-a") == {"bb_abc": b"bytes-a"}
    assert media_staging.get_all_for_user("user-b") == {"bb_abc": b"bytes-b"}
    media_staging.clear_user("user-a")
    media_staging.clear_user("user-b")


@pytest.mark.asyncio()
async def test_file_factory_merges_staged_bytes_when_current_turn_has_none(
    test_user: User,
) -> None:
    """The agent may call upload_to_storage on a turn with no attachments; staged
    bytes from an earlier turn must still be available so the upload succeeds."""
    media_staging.stage(test_user.id, "bb_photo", b"photo-bytes", "image/jpeg")

    ctx = ToolContext(
        user=test_user,
        storage=MockStorageBackend(),
        downloaded_media=[],
    )
    tools = _file_factory(ctx)
    upload = tools[0].function

    result = await upload(
        file_category="job_photo",
        description="Tile job",
        client_name="David Graham",
    )

    assert result.is_error is False
    assert "Uploaded" in result.content
    # Staging entry should be evicted after a successful upload.
    assert media_staging.get_all_for_user(test_user.id) == {}


@pytest.mark.asyncio()
async def test_file_factory_prefers_current_turn_over_stale_staging(
    test_user: User,
) -> None:
    """If the same URL is in both current downloaded_media and staging, the
    current-turn bytes win (staging is fallback only)."""
    media_staging.stage(test_user.id, "bb_photo", b"stale-bytes", "image/jpeg")
    current = DownloadedMedia(
        content=b"fresh-bytes",
        mime_type="image/jpeg",
        original_url="bb_photo",
        filename="photo.jpg",
    )
    ctx = ToolContext(
        user=test_user,
        storage=MockStorageBackend(),
        downloaded_media=[current],
    )
    tools = _file_factory(ctx)
    upload = tools[0].function

    result = await upload(
        file_category="job_photo",
        original_url="bb_photo",
        client_name="David Graham",
    )
    assert result.is_error is False
    # The fresh bytes should have been used; inspect the mock storage.
    storage = ctx.storage
    assert isinstance(storage, MockStorageBackend)
    assert any(v == b"fresh-bytes" for v in storage.files.values())


def test_get_mime_type_returns_staged_value(test_user: User) -> None:
    media_staging.stage(test_user.id, "bb_doc", b"pdf-bytes", "application/pdf")
    assert media_staging.get_mime_type(test_user.id, "bb_doc") == "application/pdf"
    assert media_staging.get_mime_type(test_user.id, "missing") is None


@pytest.mark.asyncio()
async def test_upload_uses_staged_mime_over_llm_argument(test_user: User) -> None:
    """The download layer knows the real mime; the LLM-supplied value must not
    overwrite it (e.g. agent defaults to image/jpeg but file is a PDF)."""
    media_staging.stage(test_user.id, "bb_doc", b"pdf-bytes", "application/pdf")
    ctx = ToolContext(
        user=test_user,
        storage=MockStorageBackend(),
        downloaded_media=[],
    )
    tools = _file_factory(ctx)
    upload = tools[0].function

    result = await upload(
        file_category="document",
        original_url="bb_doc",
        client_name="Jane",
        mime_type="image/jpeg",  # LLM's wrong guess
    )
    assert result.is_error is False
    # Filename should reflect the real mime (.pdf), not the LLM guess (.jpg).
    assert ".pdf" in result.content


@pytest.mark.asyncio()
async def test_upload_evicts_staged_entry(test_user: User) -> None:
    media_staging.stage(test_user.id, "bb_photo", b"bytes", "image/jpeg")
    storage = MockStorageBackend()
    tools = create_file_tools(
        test_user,
        storage,
        pending_media={"bb_photo": b"bytes"},
    )
    upload = tools[0].function

    result = await upload(
        file_category="job_photo",
        original_url="bb_photo",
        client_name="Jane",
    )
    assert result.is_error is False
    assert media_staging.get_all_for_user(test_user.id) == {}


@pytest.mark.asyncio()
async def test_auto_save_evicts_staged_entry(test_user: User) -> None:
    """Auto-save (ALWAYS permission) should also evict staged bytes."""
    media_staging.stage(test_user.id, "bb_photo", b"bytes", "image/jpeg")

    storage = MockStorageBackend()
    media = DownloadedMedia(
        content=b"bytes",
        mime_type="image/jpeg",
        original_url="bb_photo",
        filename="photo.jpg",
    )
    await auto_save_media(test_user, storage, [media])
    assert media_staging.get_all_for_user(test_user.id) == {}


class _FakeUploadParams(BaseModel):
    client_name: str = Field(default="")


@pytest.mark.asyncio()
async def test_approval_cache_coalesces_repeat_ask(test_user: User) -> None:
    """When the agent calls the same ASK tool three times with the same
    resource within one run, the user should only be prompted once."""
    calls: list[str] = []

    async def _fn(client_name: str = "") -> ToolResult:
        calls.append(client_name)
        return ToolResult(content=f"ok for {client_name}")

    tool = Tool(
        name="fake_upload",
        description="fake",
        function=_fn,
        params_model=_FakeUploadParams,
        usage_hint="",
        approval_policy=ApprovalPolicy(
            default_level=PermissionLevel.ASK,
            resource_extractor=lambda args: args.get("client_name") or None,
            description_builder=lambda args: f"Upload to {args.get('client_name')}",
        ),
    )

    # Make sure ASK is the effective level (no persisted override).
    store = get_approval_store()
    store.reset_permissions(test_user.id)

    async def _publish(_msg: object) -> None:
        return None

    gate = get_approval_gate()
    # Stub gate.request_approval so it returns APPROVED without needing a real
    # inbound response message from a channel.
    gate.request_approval = AsyncMock(return_value=ApprovalDecision.APPROVED)  # type: ignore[method-assign]

    agent = ClawboltAgent(
        user=test_user,
        channel="bluebubbles",
        publish_outbound=_publish,
        chat_id="+1234567890",
        session_id="",
    )
    agent.register_tools([tool])

    args = {"client_name": "David Graham"}
    parsed_calls = [
        ToolCallRequest(id=f"call_{i}", name="fake_upload", arguments=args) for i in range(3)
    ]
    parsed_raw = [
        ParsedToolCall(id=f"call_{i}", name="fake_upload", arguments=args) for i in range(3)
    ]

    await agent._execute_tool_round(
        parsed_calls=parsed_calls,
        parsed_raw=parsed_raw,
        actions_taken=[],
        memories_saved=[],
        tool_call_records=[],
    )

    # Three tool calls, one user approval prompt.
    assert gate.request_approval.await_count == 1  # type: ignore[attr-defined]
    assert calls == ["David Graham", "David Graham", "David Graham"]


@pytest.mark.asyncio()
async def test_always_allow_for_upload_to_storage_persists_globally(
    test_user: User,
) -> None:
    """When the user says 'Always' to an upload_to_storage prompt, the
    permission must persist globally for the tool, not scoped per client
    name (otherwise the user would have to say 'Always' separately for
    every new client they ever upload for)."""
    from backend.app.agent.llm_parsing import ParsedToolCall
    from backend.app.agent.messages import ToolCallRequest
    from backend.app.agent.tools.file_tools import create_file_tools

    store = get_approval_store()
    store.reset_permissions(test_user.id)

    gate = get_approval_gate()
    gate.request_approval = AsyncMock(return_value=ApprovalDecision.ALWAYS_ALLOW)  # type: ignore[method-assign]

    async def _publish(_msg: object) -> None:
        return None

    storage = MockStorageBackend()
    tools = create_file_tools(test_user, storage, pending_media={"bb_photo": b"bytes"})
    upload_tool = next(t for t in tools if t.name == "upload_to_storage")

    agent = ClawboltAgent(
        user=test_user,
        channel="bluebubbles",
        publish_outbound=_publish,
        chat_id="+1234567890",
        session_id="",
    )
    agent.register_tools([upload_tool])

    args = {
        "file_category": "job_photo",
        "client_name": "David Graham",
        "original_url": "bb_photo",
    }
    parsed = [ToolCallRequest(id="call_0", name="upload_to_storage", arguments=args)]
    raw = [ParsedToolCall(id="call_0", name="upload_to_storage", arguments=args)]
    await agent._execute_tool_round(
        parsed_calls=parsed,
        parsed_raw=raw,
        actions_taken=[],
        memories_saved=[],
        tool_call_records=[],
    )

    # Permission should now be ALWAYS for upload_to_storage globally, not
    # scoped to David Graham only. A subsequent upload for a different
    # client should auto-approve without another prompt.
    level_global = store.check_permission(
        test_user.id, "upload_to_storage", default=PermissionLevel.ASK
    )
    level_different_client = store.check_permission(
        test_user.id, "upload_to_storage", resource="Other Client", default=PermissionLevel.ASK
    )
    assert level_global == PermissionLevel.ALWAYS
    assert level_different_client == PermissionLevel.ALWAYS


@pytest.mark.asyncio()
async def test_always_allow_does_not_emit_synthetic_tool_record(test_user: User) -> None:
    """ALWAYS_ALLOW persists silently to the DB. The chat should NOT contain
    a synthetic tool record that mimics the agent editing PERMISSIONS.json
    -- that pattern taught the agent to double-manage the file, clobbering
    per-resource overrides. Visible feedback comes from the absence of
    future prompts and the dashboard Permissions page instead."""
    from backend.app.agent.context import StoredToolInteraction
    from backend.app.agent.llm_parsing import ParsedToolCall
    from backend.app.agent.messages import ToolCallRequest
    from backend.app.agent.tools.file_tools import create_file_tools

    store = get_approval_store()
    store.reset_permissions(test_user.id)

    gate = get_approval_gate()
    gate.request_approval = AsyncMock(return_value=ApprovalDecision.ALWAYS_ALLOW)  # type: ignore[method-assign]

    async def _publish(_msg: object) -> None:
        return None

    storage = MockStorageBackend()
    tools = create_file_tools(test_user, storage, pending_media={"bb_photo": b"bytes"})
    upload_tool = next(t for t in tools if t.name == "upload_to_storage")

    agent = ClawboltAgent(
        user=test_user,
        channel="bluebubbles",
        publish_outbound=_publish,
        chat_id="+1234567890",
        session_id="",
    )
    agent.register_tools([upload_tool])

    args = {
        "file_category": "job_photo",
        "client_name": "David Graham",
        "original_url": "bb_photo",
    }
    parsed = [ToolCallRequest(id="call_0", name="upload_to_storage", arguments=args)]
    raw = [ParsedToolCall(id="call_0", name="upload_to_storage", arguments=args)]
    records: list[StoredToolInteraction] = []
    await agent._execute_tool_round(
        parsed_calls=parsed,
        parsed_raw=raw,
        actions_taken=[],
        memories_saved=[],
        tool_call_records=records,
    )

    perm_records = [r for r in records if r.args.get("path") == "PERMISSIONS.json"]
    assert perm_records == []
    # And the permission must still be persisted.
    level = store.check_permission(test_user.id, "upload_to_storage", default=PermissionLevel.ASK)
    assert level == PermissionLevel.ALWAYS


@pytest.mark.asyncio()
async def test_always_allow_short_circuits_sibling_ask_entries_in_same_round(
    test_user: User,
) -> None:
    """If the agent fires three qb_query-style calls with the same resource in
    one round, saying 'Always' to the first should short-circuit the other
    two: they were already bucketed into ask_entries when the round started,
    so the in-memory approval cache has to absorb ALWAYS_ALLOW (not just
    APPROVED) to prevent the follow-up prompts."""
    from backend.app.agent.llm_parsing import ParsedToolCall
    from backend.app.agent.messages import ToolCallRequest

    class _QueryParams(BaseModel):
        query: str = Field(default="")

    calls: list[str] = []

    async def _fn(query: str = "") -> ToolResult:
        calls.append(query)
        return ToolResult(content=f"ran {query}")

    tool = Tool(
        name="fake_qb_query",
        description="fake",
        function=_fn,
        params_model=_QueryParams,
        usage_hint="",
        approval_policy=ApprovalPolicy(
            default_level=PermissionLevel.ASK,
            resource_extractor=lambda args: (
                "Invoice" if "Invoice" in args.get("query", "") else None
            ),
            description_builder=lambda args: f"Run {args.get('query', '')}",
        ),
    )

    store = get_approval_store()
    store.reset_permissions(test_user.id)

    async def _publish(_msg: object) -> None:
        return None

    gate = get_approval_gate()
    gate.request_approval = AsyncMock(return_value=ApprovalDecision.ALWAYS_ALLOW)  # type: ignore[method-assign]

    agent = ClawboltAgent(
        user=test_user,
        channel="bluebubbles",
        publish_outbound=_publish,
        chat_id="+1234567890",
        session_id="",
    )
    agent.register_tools([tool])

    queries = [
        "SELECT * FROM Invoice ORDERBY TxnDate DESC MAXRESULTS 5",
        "SELECT * FROM Invoice WHERE Balance > 0",
        "SELECT * FROM Invoice MAXRESULTS 10",
    ]
    parsed_calls = [
        ToolCallRequest(id=f"call_{i}", name="fake_qb_query", arguments={"query": q})
        for i, q in enumerate(queries)
    ]
    parsed_raw = [
        ParsedToolCall(id=f"call_{i}", name="fake_qb_query", arguments={"query": q})
        for i, q in enumerate(queries)
    ]

    await agent._execute_tool_round(
        parsed_calls=parsed_calls,
        parsed_raw=parsed_raw,
        actions_taken=[],
        memories_saved=[],
        tool_call_records=[],
    )

    # Three calls, same resource (Invoice) → user prompted exactly once.
    assert gate.request_approval.await_count == 1  # type: ignore[attr-defined]
    assert calls == queries  # all three ran


@pytest.mark.asyncio()
async def test_denied_short_circuits_sibling_ask_entries(test_user: User) -> None:
    """Symmetric: if the agent fires the same tool+resource multiple times
    and the user says 'no', we shouldn't re-prompt for the siblings."""
    from backend.app.agent.llm_parsing import ParsedToolCall
    from backend.app.agent.messages import ToolCallRequest

    class _Params(BaseModel):
        x: str = Field(default="")

    async def _fn(x: str = "") -> ToolResult:
        return ToolResult(content=f"ran {x}")

    tool = Tool(
        name="fake_tool",
        description="fake",
        function=_fn,
        params_model=_Params,
        usage_hint="",
        approval_policy=ApprovalPolicy(
            default_level=PermissionLevel.ASK,
            resource_extractor=lambda args: args.get("x") or None,
            description_builder=lambda args: f"Run {args.get('x', '')}",
        ),
    )

    store = get_approval_store()
    store.reset_permissions(test_user.id)

    async def _publish(_msg: object) -> None:
        return None

    gate = get_approval_gate()
    gate.request_approval = AsyncMock(return_value=ApprovalDecision.DENIED)  # type: ignore[method-assign]

    agent = ClawboltAgent(
        user=test_user,
        channel="bluebubbles",
        publish_outbound=_publish,
        chat_id="+1234567890",
        session_id="",
    )
    agent.register_tools([tool])

    args = {"x": "shared"}
    parsed_calls = [
        ToolCallRequest(id=f"call_{i}", name="fake_tool", arguments=args) for i in range(3)
    ]
    parsed_raw = [
        ParsedToolCall(id=f"call_{i}", name="fake_tool", arguments=args) for i in range(3)
    ]

    await agent._execute_tool_round(
        parsed_calls=parsed_calls,
        parsed_raw=parsed_raw,
        actions_taken=[],
        memories_saved=[],
        tool_call_records=[],
    )

    assert gate.request_approval.await_count == 1  # type: ignore[attr-defined]


@pytest.mark.asyncio()
async def test_permissions_json_shared_between_approval_store_and_workspace(
    test_user: User,
) -> None:
    """ApprovalStore and read_file/write_file must hit the same DB row so
    the chat vocabulary ('PERMISSIONS.json') and the typed approval API
    can't drift apart."""
    from backend.app.agent.tools.workspace_tools import create_workspace_tools

    store = get_approval_store()
    store.reset_permissions(test_user.id)
    store.set_permission(test_user.id, "qb_query", PermissionLevel.ALWAYS, resource="Invoice")

    tools = create_workspace_tools(test_user.id)
    read_tool = next(t for t in tools if t.name == "read_file")
    write_tool = next(t for t in tools if t.name == "write_file")

    result = await read_tool.function(path="PERMISSIONS.json")
    assert result.is_error is False
    assert '"qb_query"' in result.content
    assert '"Invoice": "always"' in result.content

    # Editing through workspace_tools must flow back into ApprovalStore.
    overridden = result.content.replace('"Invoice": "always"', '"Invoice": "deny"')
    await write_tool.function(path="PERMISSIONS.json", content=overridden)

    level = store.check_permission(
        test_user.id, "qb_query", resource="Invoice", default=PermissionLevel.ASK
    )
    assert level == PermissionLevel.DENY


@pytest.mark.asyncio()
async def test_permissions_write_normalizes_minified_json(test_user: User) -> None:
    """If the agent writes minified PERMISSIONS.json (no newlines) via
    write_file, the store should normalize it to indented JSON so
    subsequent edit_file calls have stable formatting to match against.
    Regression for the bug where one minified write broke every later
    edit_file because old_text assumed pretty-printed content."""
    from backend.app.agent.tools.workspace_tools import create_workspace_tools

    store = get_approval_store()
    store.reset_permissions(test_user.id)

    tools = create_workspace_tools(test_user.id)
    write_tool = next(t for t in tools if t.name == "write_file")
    read_tool = next(t for t in tools if t.name == "read_file")

    minified = '{"version": 1, "tools": {"qb_query": "always"}, "resources": {}}'
    await write_tool.function(path="PERMISSIONS.json", content=minified)

    result = await read_tool.function(path="PERMISSIONS.json")
    assert result.is_error is False
    # Indented form has newlines and leading spaces on nested keys.
    assert "\n" in result.content
    assert '  "tools"' in result.content
    assert '"qb_query": "always"' in result.content
