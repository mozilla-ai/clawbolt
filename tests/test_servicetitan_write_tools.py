"""Tests for the ServiceTitan write tools.

The first write tool is ``st_add_job_note``. It is exercised end-to-end
against the in-process fake backend, plus a few static checks on the
``Tool`` definition (approval policy, concurrency group) to lock in the
runtime contract the agent depends on for permission gating and
parallel-tool serialization.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from backend.app.agent.approval import PermissionLevel
from backend.app.agent.tools.names import ToolName
from backend.app.agent.tools.servicetitan_tools import build_servicetitan_tools
from backend.app.config import settings as _settings
from backend.app.integrations.servicetitan import _fake as fake_module
from backend.app.integrations.servicetitan._fake import (
    ServiceTitanFakeBackend,
)
from backend.app.integrations.servicetitan._fake import (
    build_fake_transport as real_build_fake_transport,
)
from backend.app.integrations.servicetitan.auth import save_credentials
from backend.app.integrations.servicetitan.params import StAddJobNoteParams
from backend.app.integrations.servicetitan.service import (
    ServiceTitanService,
    build_service_for_user,
)


@pytest.fixture()
def shared_backend() -> Any:
    """A fresh fake backend bound across every service request in the test.

    ``service.py``'s ``_build_http_client`` calls ``build_fake_transport()``
    with no argument, which constructs a new backend per request, so a
    POST and a follow-up inspection cannot see each other's state. The
    write-tool tests need cross-request state to confirm the note
    persisted, so we patch the transport factory to bind every request
    to the same backend instance for the duration of the test.
    """
    backend = ServiceTitanFakeBackend()

    def _build(_backend: Any = None) -> Any:
        return real_build_fake_transport(backend)

    with patch(
        "backend.app.integrations.servicetitan.service.build_fake_transport",
        side_effect=_build,
    ):
        yield backend


@pytest.fixture(autouse=True)
def _force_fake_backend(shared_backend: Any) -> Any:
    """Route every test in this module through the in-process fake backend.

    Resets the process-wide default fake between tests so a previous
    note POST cannot leak via any other code path that uses
    ``get_default_fake_backend`` directly. The credential row is cleaned
    by the standard async test isolation fixture.
    """
    with patch.object(_settings, "servicetitan_use_fake", True):
        fake_module.reset_default_fake_backend()
        try:
            yield
        finally:
            fake_module.reset_default_fake_backend()


def _connected_credential_kwargs() -> dict[str, Any]:
    return {
        "tenant_id": str(fake_module.DEFAULT_TENANT_ID),
        "client_id": "cid",
        "client_secret": "csec",
        "app_key": "fake-st-app-key",
        "access_token": fake_module.FAKE_TOKEN_VALUE,
        "expires_at": time.time() + 600,
    }


async def _build_connected_service(user_id: str) -> ServiceTitanService:
    await save_credentials(user_id, **_connected_credential_kwargs())
    service = await build_service_for_user(user_id)
    assert service is not None
    return service


def _tool_by_name(tools: list[Any], name: str) -> Any:
    for t in tools:
        if t.name == name:
            return t
    raise AssertionError(f"tool {name} not found")


# ---------------------------------------------------------------------------
# Tool definition: approval policy + concurrency group
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_add_job_note_has_ask_approval_policy(async_test_user: Any) -> None:
    """The write tool must declare an ApprovalPolicy with default ASK.

    Without this, the dashboard would surface the SubToolInfo's
    ``default_permission='ask'`` but core.py would short-circuit to
    ALWAYS at runtime, auto-executing the write without user consent.
    """
    service = await _build_connected_service(async_test_user.id)
    tool = _tool_by_name(build_servicetitan_tools(service), ToolName.SERVICETITAN_ADD_JOB_NOTE)
    assert tool.approval_policy is not None
    assert tool.approval_policy.default_level == PermissionLevel.ASK


@pytest.mark.asyncio()
async def test_add_job_note_description_builder_quotes_job_id(
    async_test_user: Any,
) -> None:
    """The approval prompt must reference the job id so the user has context."""
    service = await _build_connected_service(async_test_user.id)
    tool = _tool_by_name(build_servicetitan_tools(service), ToolName.SERVICETITAN_ADD_JOB_NOTE)
    policy = tool.approval_policy
    assert policy is not None and policy.description_builder is not None
    prompt = policy.description_builder({"job_id": 2001, "text": "foo"})
    assert "2001" in prompt
    pinned_prompt = policy.description_builder({"job_id": 2001, "text": "foo", "pin_to_top": True})
    assert "pinned" in pinned_prompt.lower()


@pytest.mark.asyncio()
async def test_add_job_note_resource_extractor_returns_per_job_key(
    async_test_user: Any,
) -> None:
    """resource_extractor lets stored approvals be scoped to a single job."""
    service = await _build_connected_service(async_test_user.id)
    tool = _tool_by_name(build_servicetitan_tools(service), ToolName.SERVICETITAN_ADD_JOB_NOTE)
    policy = tool.approval_policy
    assert policy is not None and policy.resource_extractor is not None
    assert policy.resource_extractor({"job_id": 2001}) == "job:2001"
    assert policy.resource_extractor({}) is None


@pytest.mark.asyncio()
async def test_add_job_note_has_concurrency_group(async_test_user: Any) -> None:
    """The write tool must declare a concurrency group.

    The agent runs approved tool calls from a single LLM turn in
    parallel by default. A note write must serialize with any other
    ServiceTitan write so two concurrent posts cannot race on the
    same tenant.
    """
    service = await _build_connected_service(async_test_user.id)
    tool = _tool_by_name(build_servicetitan_tools(service), ToolName.SERVICETITAN_ADD_JOB_NOTE)
    assert tool.concurrency_group == "user_st_writes"


# ---------------------------------------------------------------------------
# Params validation
# ---------------------------------------------------------------------------


def test_params_reject_blank_text() -> None:
    """Whitespace-only text is rejected before the request is issued."""
    with pytest.raises(ValidationError):
        StAddJobNoteParams(job_id=2001, text="   ")


def test_params_reject_empty_text() -> None:
    """Pydantic's ``min_length=1`` rejects an empty string."""
    with pytest.raises(ValidationError):
        StAddJobNoteParams(job_id=2001, text="")


def test_params_accept_minimal_inputs() -> None:
    """Happy path: job_id + non-blank text is enough; pin_to_top defaults to False."""
    params = StAddJobNoteParams(job_id=2001, text="Customer confirmed time")
    assert params.job_id == 2001
    assert params.text == "Customer confirmed time"
    assert params.pin_to_top is False


# ---------------------------------------------------------------------------
# End-to-end against the fake
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_add_job_note_happy_path_posts_to_fake(
    async_test_user: Any, shared_backend: Any
) -> None:
    """A successful POST persists the note and returns a receipt."""
    service = await _build_connected_service(async_test_user.id)
    add_note = _tool_by_name(build_servicetitan_tools(service), ToolName.SERVICETITAN_ADD_JOB_NOTE)

    note_text = "Customer confirmed 9am Tuesday."
    result = await add_note.function(job_id=2001, text=note_text)
    assert result.is_error is False
    assert "2001" in result.content
    assert result.receipt is not None
    assert "ServiceTitan" in result.receipt.action
    assert "2001" in result.receipt.target

    # The fake persists notes on the job's ``_notes`` list. Read it back
    # off the shared backend so we do not depend on a separate read
    # endpoint that the agent has not exposed yet.
    job = next(j for j in shared_backend.jobs if j["id"] == 2001)
    assert job.get("_notes"), "fake did not persist the posted note"
    persisted = job["_notes"][-1]
    assert persisted["text"] == note_text
    assert persisted["isPinned"] is False


@pytest.mark.asyncio()
async def test_add_job_note_pin_to_top_propagates(
    async_test_user: Any, shared_backend: Any
) -> None:
    """The pin_to_top flag flows through to the API body and persists."""
    service = await _build_connected_service(async_test_user.id)
    add_note = _tool_by_name(build_servicetitan_tools(service), ToolName.SERVICETITAN_ADD_JOB_NOTE)

    result = await add_note.function(
        job_id=2001,
        text="Read before next visit",
        pin_to_top=True,
    )
    assert result.is_error is False
    assert "pinned" in result.content.lower()

    job = next(j for j in shared_backend.jobs if j["id"] == 2001)
    persisted = job["_notes"][-1]
    assert persisted["isPinned"] is True


@pytest.mark.asyncio()
async def test_add_job_note_unknown_job_returns_not_found(
    async_test_user: Any,
) -> None:
    """A 404 from the fake surfaces as a NOT_FOUND ToolResult, not SERVICE."""
    service = await _build_connected_service(async_test_user.id)
    add_note = _tool_by_name(build_servicetitan_tools(service), ToolName.SERVICETITAN_ADD_JOB_NOTE)

    result = await add_note.function(job_id=999999, text="anything")
    assert result.is_error is True
    assert result.error_kind is not None
    assert result.error_kind.value == "not_found"
    assert "999999" in result.content


@pytest.mark.asyncio()
async def test_add_job_note_blank_text_validation_error(async_test_user: Any) -> None:
    """The tool function rejects whitespace-only text up front.

    Direct callers that bypass the params model (e.g. test code, future
    pipeline wiring) still get a clear VALIDATION error rather than a
    400 surfaced as a generic SERVICE failure.
    """
    service = await _build_connected_service(async_test_user.id)
    add_note = _tool_by_name(build_servicetitan_tools(service), ToolName.SERVICETITAN_ADD_JOB_NOTE)

    result = await add_note.function(job_id=2001, text="   ")
    assert result.is_error is True
    assert result.error_kind is not None
    assert result.error_kind.value == "validation"
