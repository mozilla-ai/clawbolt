"""Tests for the composable inbound message pipeline."""

from __future__ import annotations

import pytest

from backend.app.agent.router import (
    DEFAULT_PIPELINE,
    PipelineContext,
    PipelineStep,
    build_context_step,
    dispatch_reply_step,
    finalize_onboarding_step,
    load_history_step,
    persist_outbound_step,
    prepare_media_step,
    run_agent_step,
    run_pipeline,
)


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
        db=None,  # type: ignore[arg-type]
        contractor=None,  # type: ignore[arg-type]
        message=None,  # type: ignore[arg-type]
        media_urls=[],
        messaging_service=None,  # type: ignore[arg-type]
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
        db=None,  # type: ignore[arg-type]
        contractor=None,  # type: ignore[arg-type]
        message=None,  # type: ignore[arg-type]
        media_urls=[],
        messaging_service=None,  # type: ignore[arg-type]
    )

    result = await run_pipeline(ctx, [tracking_step, tracking_step])
    assert result is ctx
    assert all(c is ctx for c in seen_contexts)


@pytest.mark.asyncio
async def test_run_pipeline_empty_steps() -> None:
    """An empty pipeline should return the context unchanged."""
    ctx = PipelineContext(
        db=None,  # type: ignore[arg-type]
        contractor=None,  # type: ignore[arg-type]
        message=None,  # type: ignore[arg-type]
        media_urls=[],
        messaging_service=None,  # type: ignore[arg-type]
    )
    result = await run_pipeline(ctx, [])
    assert result is ctx


def test_default_pipeline_contains_all_steps() -> None:
    """DEFAULT_PIPELINE should include all seven standard steps in the correct order."""
    expected: list[PipelineStep] = [
        prepare_media_step,
        build_context_step,
        load_history_step,
        run_agent_step,
        finalize_onboarding_step,
        dispatch_reply_step,
        persist_outbound_step,
    ]
    assert expected == DEFAULT_PIPELINE


def test_default_pipeline_length() -> None:
    """DEFAULT_PIPELINE should have exactly 7 steps."""
    assert len(DEFAULT_PIPELINE) == 7


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
        db=None,  # type: ignore[arg-type]
        contractor=None,  # type: ignore[arg-type]
        message=None,  # type: ignore[arg-type]
        media_urls=[],
        messaging_service=None,  # type: ignore[arg-type]
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
        db=None,  # type: ignore[arg-type]
        contractor=None,  # type: ignore[arg-type]
        message=None,  # type: ignore[arg-type]
        media_urls=[],
        messaging_service=None,  # type: ignore[arg-type]
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
        db=None,  # type: ignore[arg-type]
        contractor=None,  # type: ignore[arg-type]
        message=None,  # type: ignore[arg-type]
        media_urls=[],
        messaging_service=None,  # type: ignore[arg-type]
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
        db=None,  # type: ignore[arg-type]
        contractor=None,  # type: ignore[arg-type]
        message=None,  # type: ignore[arg-type]
        media_urls=[],
        messaging_service=None,  # type: ignore[arg-type]
    )

    await run_pipeline(ctx, [set_context, check_context])
