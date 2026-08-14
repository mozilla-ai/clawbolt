"""Tests for premium pipeline construction (#202)."""

from backend.app.agent.router import DEFAULT_PIPELINE, run_agent_step
from backend.app.billing.pipeline_steps import (
    check_quota_step,
    get_multi_user_pipeline,
    guarded_run_agent_step,
    track_usage_step,
)


class TestPremiumPipeline:
    def test_contains_all_default_steps_except_run_agent(self) -> None:
        """Premium pipeline should contain every OSS step except run_agent_step."""
        pipeline = get_multi_user_pipeline()
        for step in DEFAULT_PIPELINE:
            if step is run_agent_step:
                assert step not in pipeline
            else:
                assert step in pipeline

    def test_contains_premium_steps(self) -> None:
        """Premium pipeline should include quota check, guarded agent, and usage tracking."""
        pipeline = get_multi_user_pipeline()
        assert check_quota_step in pipeline
        assert guarded_run_agent_step in pipeline
        assert track_usage_step in pipeline

    def test_quota_check_before_agent(self) -> None:
        """check_quota_step must come before guarded_run_agent_step."""
        pipeline = get_multi_user_pipeline()
        assert pipeline.index(check_quota_step) < pipeline.index(guarded_run_agent_step)

    def test_track_usage_after_persist(self) -> None:
        """track_usage_step must come after persist_outbound_step."""
        from backend.app.agent.router import persist_outbound_step

        pipeline = get_multi_user_pipeline()
        assert pipeline.index(persist_outbound_step) < pipeline.index(track_usage_step)
