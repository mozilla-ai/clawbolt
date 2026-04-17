"""Tests for the composable inbound message pipeline."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from backend.app.agent.router import (
    DEFAULT_PIPELINE,
    PipelineContext,
    PipelineStep,
    build_context_step,
    build_pipeline,
    dispatch_reply_step,
    finalize_onboarding_step,
    load_history_step,
    persist_outbound_step,
    persist_system_prompt_step,
    prepare_media_step,
    run_agent_step,
    run_pipeline,
)
from backend.app.media.download import DownloadedMedia

if TYPE_CHECKING:
    from backend.app.agent.core import AgentResponse


@pytest.mark.asyncio
async def test_run_pipeline_executes_steps_in_order() -> None:
    """Steps should execute sequentially, each receiving the context from the prior step."""
    call_order: list[str] = []

    async def step_a(ctx: PipelineContext) -> PipelineContext:
        call_order.append("a")
        return ctx

    async def step_b(ctx: PipelineContext) -> PipelineContext:
        call_order.append("b")
        return ctx

    async def step_c(ctx: PipelineContext) -> PipelineContext:
        call_order.append("c")
        return ctx

    # Use a minimal context; fields are unused by these test steps
    ctx = PipelineContext(
        user=None,  # type: ignore[arg-type]
        session=None,  # type: ignore[arg-type]
        message=None,  # type: ignore[arg-type]
        media_urls=[],
        channel="telegram",
    )

    await run_pipeline(ctx, [step_a, step_b, step_c])
    assert call_order == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_run_pipeline_passes_context_through() -> None:
    """Each step should receive and return the same context object."""
    seen_contexts: list[PipelineContext] = []

    async def tracking_step(ctx: PipelineContext) -> PipelineContext:
        seen_contexts.append(ctx)
        return ctx

    ctx = PipelineContext(
        user=None,  # type: ignore[arg-type]
        session=None,  # type: ignore[arg-type]
        message=None,  # type: ignore[arg-type]
        media_urls=[],
        channel="telegram",
    )

    result = await run_pipeline(ctx, [tracking_step, tracking_step])
    assert result is ctx
    assert all(c is ctx for c in seen_contexts)


@pytest.mark.asyncio
async def test_run_pipeline_empty_steps() -> None:
    """An empty pipeline should return the context unchanged."""
    ctx = PipelineContext(
        user=None,  # type: ignore[arg-type]
        session=None,  # type: ignore[arg-type]
        message=None,  # type: ignore[arg-type]
        media_urls=[],
        channel="telegram",
    )
    result = await run_pipeline(ctx, [])
    assert result is ctx


def test_default_pipeline_contains_all_steps() -> None:
    """DEFAULT_PIPELINE should include all eight standard steps in the correct order."""
    expected: list[PipelineStep] = [
        prepare_media_step,
        build_context_step,
        load_history_step,
        run_agent_step,
        persist_system_prompt_step,
        finalize_onboarding_step,
        dispatch_reply_step,
        persist_outbound_step,
    ]
    assert expected == DEFAULT_PIPELINE


def test_default_pipeline_length() -> None:
    """DEFAULT_PIPELINE should have exactly 8 steps."""
    assert len(DEFAULT_PIPELINE) == 8


@pytest.mark.asyncio
async def test_custom_pipeline_can_skip_steps() -> None:
    """A custom pipeline can omit steps from the default."""
    call_order: list[str] = []

    async def mock_prepare(ctx: PipelineContext) -> PipelineContext:
        call_order.append("prepare")
        return ctx

    async def mock_agent(ctx: PipelineContext) -> PipelineContext:
        call_order.append("agent")
        return ctx

    ctx = PipelineContext(
        user=None,  # type: ignore[arg-type]
        session=None,  # type: ignore[arg-type]
        message=None,  # type: ignore[arg-type]
        media_urls=[],
        channel="telegram",
    )

    await run_pipeline(ctx, [mock_prepare, mock_agent])
    assert call_order == ["prepare", "agent"]


@pytest.mark.asyncio
async def test_custom_pipeline_can_add_steps() -> None:
    """A custom pipeline can inject extra steps between default ones."""
    call_order: list[str] = []

    async def step_default(ctx: PipelineContext) -> PipelineContext:
        call_order.append("default")
        return ctx

    async def step_custom(ctx: PipelineContext) -> PipelineContext:
        call_order.append("custom")
        return ctx

    ctx = PipelineContext(
        user=None,  # type: ignore[arg-type]
        session=None,  # type: ignore[arg-type]
        message=None,  # type: ignore[arg-type]
        media_urls=[],
        channel="telegram",
    )

    await run_pipeline(ctx, [step_default, step_custom, step_default])
    assert call_order == ["default", "custom", "default"]


@pytest.mark.asyncio
async def test_custom_pipeline_can_reorder_steps() -> None:
    """A custom pipeline can reorder steps."""
    call_order: list[str] = []

    async def step_x(ctx: PipelineContext) -> PipelineContext:
        call_order.append("x")
        return ctx

    async def step_y(ctx: PipelineContext) -> PipelineContext:
        call_order.append("y")
        return ctx

    async def step_z(ctx: PipelineContext) -> PipelineContext:
        call_order.append("z")
        return ctx

    ctx = PipelineContext(
        user=None,  # type: ignore[arg-type]
        session=None,  # type: ignore[arg-type]
        message=None,  # type: ignore[arg-type]
        media_urls=[],
        channel="telegram",
    )

    # Reverse order
    await run_pipeline(ctx, [step_z, step_y, step_x])
    assert call_order == ["z", "y", "x"]


@pytest.mark.asyncio
async def test_pipeline_step_can_mutate_context() -> None:
    """A step should be able to set fields on the context for later steps."""

    async def set_context(ctx: PipelineContext) -> PipelineContext:
        ctx.combined_context = "hello from step"
        return ctx

    async def check_context(ctx: PipelineContext) -> PipelineContext:
        assert ctx.combined_context == "hello from step"
        return ctx

    ctx = PipelineContext(
        user=None,  # type: ignore[arg-type]
        session=None,  # type: ignore[arg-type]
        message=None,  # type: ignore[arg-type]
        media_urls=[],
        channel="telegram",
    )

    await run_pipeline(ctx, [set_context, check_context])


@pytest.mark.asyncio
async def test_prepare_media_step_preserves_pre_downloaded_media() -> None:
    """prepare_media_step must not discard already-downloaded media.

    Webchat uploads arrive as pre-populated ``downloaded_media`` on the
    context (no ``media_urls``). Before the fix, the step overwrote
    ``ctx.downloaded_media`` with the empty result of ``prepare_media()``,
    silently dropping webchat image uploads.

    Regression test for https://github.com/mozilla-ai/clawbolt/issues/664
    """
    from unittest.mock import AsyncMock

    pre_downloaded = DownloadedMedia(
        content=b"fake-image-bytes",
        mime_type="image/png",
        original_url="upload://photo.png",
        filename="photo.png",
    )

    ctx = PipelineContext(
        user=None,  # type: ignore[arg-type]
        session=None,  # type: ignore[arg-type]
        message=None,  # type: ignore[arg-type]
        media_urls=[],
        channel="webchat",
        downloaded_media=[pre_downloaded],
    )

    with patch(
        "backend.app.agent.router.prepare_media",
        new_callable=AsyncMock,
        return_value=([], None),
    ):
        result = await prepare_media_step(ctx)

    assert len(result.downloaded_media) == 1
    assert result.downloaded_media[0] is pre_downloaded


# ---------------------------------------------------------------------------
# dispatch_reply_step: receipt rendering on plain-text channels
# ---------------------------------------------------------------------------


def _make_response_with_receipt(
    *,
    reply_text: str,
    tool_name: str,
    action: str,
    target: str,
    url: str | None = None,
    is_error: bool = False,
) -> AgentResponse:
    """Build an AgentResponse with a single tool call that may carry a receipt."""
    from backend.app.agent.context import StoredToolInteraction, StoredToolReceipt
    from backend.app.agent.core import AgentResponse

    receipt = None if is_error else StoredToolReceipt(action=action, target=target, url=url)
    return AgentResponse(
        reply_text=reply_text,
        tool_calls=[
            StoredToolInteraction(
                tool_call_id="tc-1",
                name=tool_name,
                args={},
                result="ok",
                is_error=is_error,
                receipt=receipt,
            )
        ],
    )


def _make_response_without_receipt(
    *,
    reply_text: str,
    tool_name: str,
    is_error: bool = False,
) -> AgentResponse:
    """Build an AgentResponse for a read-side tool that has no receipt."""
    from backend.app.agent.context import StoredToolInteraction
    from backend.app.agent.core import AgentResponse

    return AgentResponse(
        reply_text=reply_text,
        tool_calls=[
            StoredToolInteraction(
                tool_call_id="tc-1",
                name=tool_name,
                args={},
                result="ok",
                is_error=is_error,
                receipt=None,
            )
        ],
    )


def _make_ctx(
    *, channel: str, response: AgentResponse, to_address: str = "+15555555555"
) -> PipelineContext:
    return PipelineContext(
        user=None,  # type: ignore[arg-type]
        session=None,  # type: ignore[arg-type]
        message=None,  # type: ignore[arg-type]
        media_urls=[],
        channel=channel,
        to_address=to_address,
        response=response,
    )


@pytest.mark.asyncio
async def test_dispatch_reply_appends_receipt_for_imessage_write_tool() -> None:
    """On plain-text channels (bluebubbles iMessage), the outbound body
    must carry a deterministic receipt line generated from real tool
    output, not anything the LLM said."""
    from unittest.mock import AsyncMock

    response = _make_response_with_receipt(
        reply_text="Kitchen demo looks good.",
        tool_name="companycam_upload_photo",
        action="Uploaded photo to CompanyCam project",
        target="Davis",
        url="https://companycam.com/p/abc123",
    )
    ctx = _make_ctx(channel="bluebubbles", response=response)

    with patch(
        "backend.app.bus.message_bus.publish_outbound",
        new_callable=AsyncMock,
    ) as mock_publish:
        await dispatch_reply_step(ctx)

    assert mock_publish.await_count == 1
    assert mock_publish.await_args is not None
    outbound = mock_publish.await_args.args[0]
    assert "Kitchen demo looks good." in outbound.content
    assert "- Uploaded photo to CompanyCam project Davis" in outbound.content
    assert "https://companycam.com/p/abc123" in outbound.content


@pytest.mark.asyncio
async def test_dispatch_reply_also_appends_receipt_for_webchat() -> None:
    """Receipts now ship on every channel, including the web dashboard,
    so the admin chat and the contractor's iMessage thread show the
    same evidence of what actually happened."""
    from unittest.mock import AsyncMock

    response = _make_response_with_receipt(
        reply_text="Done.",
        tool_name="companycam_upload_photo",
        action="Uploaded photo to CompanyCam project",
        target="Davis",
        url="https://companycam.com/p/abc123",
    )
    ctx = _make_ctx(channel="webchat", response=response, to_address="user-1")
    ctx.request_id = "req-1"

    with patch(
        "backend.app.bus.message_bus.publish_outbound",
        new_callable=AsyncMock,
    ) as mock_publish:
        await dispatch_reply_step(ctx)

    assert mock_publish.await_count == 1
    assert mock_publish.await_args is not None
    outbound = mock_publish.await_args.args[0]
    assert outbound.content.startswith("Done.")
    assert "- Uploaded photo to CompanyCam project Davis" in outbound.content
    assert "https://companycam.com/p/abc123" in outbound.content


@pytest.mark.asyncio
async def test_dispatch_reply_omits_receipt_for_failed_mutation() -> None:
    """A mutation that errored did NOT actually happen. The receipt
    block must not imply success; failures live in the reply text."""
    from unittest.mock import AsyncMock

    response = _make_response_with_receipt(
        reply_text="QuickBooks logged me out. Can you reconnect?",
        tool_name="qb_create",
        action="Created QuickBooks invoice for",
        target="Johnson",
        is_error=True,
    )
    ctx = _make_ctx(channel="bluebubbles", response=response)

    with patch(
        "backend.app.bus.message_bus.publish_outbound",
        new_callable=AsyncMock,
    ) as mock_publish:
        await dispatch_reply_step(ctx)

    assert mock_publish.await_count == 1
    assert mock_publish.await_args is not None
    outbound = mock_publish.await_args.args[0]
    assert outbound.content == "QuickBooks logged me out. Can you reconnect?"
    assert "- Created" not in outbound.content


@pytest.mark.asyncio
async def test_dispatch_reply_omits_receipt_for_read_tool() -> None:
    """Read-side tools (qb_query, calendar_list_events, memory recall)
    return data which is self-verifying. They don't populate a receipt
    and must not produce a footer line."""
    from unittest.mock import AsyncMock

    response = _make_response_without_receipt(
        reply_text="Davis estimate total is $2,360.",
        tool_name="qb_query",
    )
    ctx = _make_ctx(channel="bluebubbles", response=response)

    with patch(
        "backend.app.bus.message_bus.publish_outbound",
        new_callable=AsyncMock,
    ) as mock_publish:
        await dispatch_reply_step(ctx)

    assert mock_publish.await_count == 1
    assert mock_publish.await_args is not None
    outbound = mock_publish.await_args.args[0]
    assert outbound.content == "Davis estimate total is $2,360."


# ---------------------------------------------------------------------------
# build_pipeline() tests
# ---------------------------------------------------------------------------


async def _noop_step(ctx: PipelineContext) -> PipelineContext:
    return ctx


def test_build_pipeline_no_modifications() -> None:
    """build_pipeline() with no args returns a copy of DEFAULT_PIPELINE."""
    result = build_pipeline()
    assert result == DEFAULT_PIPELINE
    assert result is not DEFAULT_PIPELINE


def test_build_pipeline_replace() -> None:
    """build_pipeline(replace=...) should swap a step."""
    result = build_pipeline(replace={run_agent_step: _noop_step})
    assert _noop_step in result
    assert run_agent_step not in result
    assert len(result) == len(DEFAULT_PIPELINE)


def test_build_pipeline_insert_before() -> None:
    """build_pipeline(insert_before=...) should inject steps before a target."""
    result = build_pipeline(insert_before={run_agent_step: [_noop_step]})
    idx_noop = result.index(_noop_step)
    idx_agent = result.index(run_agent_step)
    assert idx_noop == idx_agent - 1
    assert len(result) == len(DEFAULT_PIPELINE) + 1


def test_build_pipeline_insert_after() -> None:
    """build_pipeline(insert_after=...) should inject steps after a target."""
    result = build_pipeline(insert_after={persist_outbound_step: [_noop_step]})
    idx_persist = result.index(persist_outbound_step)
    idx_noop = result.index(_noop_step)
    assert idx_noop == idx_persist + 1
    assert len(result) == len(DEFAULT_PIPELINE) + 1


def test_build_pipeline_combined() -> None:
    """build_pipeline with replace + insert_before + insert_after."""

    async def quota_step(ctx: PipelineContext) -> PipelineContext:
        return ctx

    async def guarded_agent(ctx: PipelineContext) -> PipelineContext:
        return ctx

    async def track_step(ctx: PipelineContext) -> PipelineContext:
        return ctx

    result = build_pipeline(
        insert_before={run_agent_step: [quota_step]},
        replace={run_agent_step: guarded_agent},
        insert_after={persist_outbound_step: [track_step]},
    )

    # All default steps except run_agent_step should still be present
    for step in DEFAULT_PIPELINE:
        if step is not run_agent_step:
            assert step in result

    # The three injected steps should be present
    assert quota_step in result
    assert guarded_agent in result
    assert track_step in result

    # Order: quota_step before guarded_agent, track_step after persist_outbound
    assert result.index(quota_step) < result.index(guarded_agent)
    assert result.index(persist_outbound_step) < result.index(track_step)
