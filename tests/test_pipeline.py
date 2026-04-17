"""Tests for the composable inbound message pipeline."""

from __future__ import annotations

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
# dispatch_reply_step: tool-call summary routing
# ---------------------------------------------------------------------------


async def _drain_outbound() -> list:
    """Pull every queued OutboundMessage so the test sees a clean bus."""
    from backend.app.bus import message_bus

    drained = []
    while not message_bus.outbound.empty():
        drained.append(message_bus.outbound.get_nowait())
    return drained


@pytest.mark.asyncio
async def test_dispatch_reply_appends_tool_summary_for_imessage() -> None:
    """On plain-text channels (bluebubbles iMessage), the outbound body
    must carry a deterministic tool-call summary so the user can see what
    actually ran, independent of whatever the LLM said in text."""
    from backend.app.agent.context import StoredToolInteraction
    from backend.app.agent.core import AgentResponse

    await _drain_outbound()

    response = AgentResponse(
        reply_text="Done uploading your photo.",
        tool_calls=[
            StoredToolInteraction(
                tool_call_id="tc-1",
                name="companycam_upload_photo",
                args={},
                result="ok",
                is_error=False,
            )
        ],
    )
    ctx = PipelineContext(
        user=None,  # type: ignore[arg-type]
        session=None,  # type: ignore[arg-type]
        message=None,  # type: ignore[arg-type]
        media_urls=[],
        channel="bluebubbles",
        to_address="+15555555555",
        response=response,
    )

    await dispatch_reply_step(ctx)

    outbound = await _drain_outbound()
    assert len(outbound) == 1
    assert "Done uploading your photo." in outbound[0].content
    assert "Tools used: companycam_upload_photo" in outbound[0].content


@pytest.mark.asyncio
async def test_dispatch_reply_does_not_append_summary_for_webchat() -> None:
    """Webchat renders a structured tool-call panel from the stored
    tool_interactions_json, so the plain-text summary would duplicate
    that UI and clutter the chat."""
    from backend.app.agent.context import StoredToolInteraction
    from backend.app.agent.core import AgentResponse

    await _drain_outbound()

    response = AgentResponse(
        reply_text="Done.",
        tool_calls=[
            StoredToolInteraction(
                tool_call_id="tc-1",
                name="companycam_upload_photo",
                args={},
                result="ok",
                is_error=False,
            )
        ],
    )
    ctx = PipelineContext(
        user=None,  # type: ignore[arg-type]
        session=None,  # type: ignore[arg-type]
        message=None,  # type: ignore[arg-type]
        media_urls=[],
        channel="webchat",
        to_address="user-1",
        response=response,
        request_id="req-1",
    )

    await dispatch_reply_step(ctx)

    outbound = await _drain_outbound()
    assert len(outbound) == 1
    assert outbound[0].content == "Done."


@pytest.mark.asyncio
async def test_dispatch_reply_flags_failed_tools_in_summary() -> None:
    """Failed tool calls must be annotated so the user can see when a
    declared action did not actually complete."""
    from backend.app.agent.context import StoredToolInteraction
    from backend.app.agent.core import AgentResponse

    await _drain_outbound()

    response = AgentResponse(
        reply_text="Working on it.",
        tool_calls=[
            StoredToolInteraction(
                tool_call_id="tc-1",
                name="qb_create_invoice",
                args={},
                result="quickbooks auth expired",
                is_error=True,
            )
        ],
    )
    ctx = PipelineContext(
        user=None,  # type: ignore[arg-type]
        session=None,  # type: ignore[arg-type]
        message=None,  # type: ignore[arg-type]
        media_urls=[],
        channel="bluebubbles",
        to_address="+15555555555",
        response=response,
    )

    await dispatch_reply_step(ctx)

    outbound = await _drain_outbound()
    assert len(outbound) == 1
    assert "qb_create_invoice (failed)" in outbound[0].content


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
