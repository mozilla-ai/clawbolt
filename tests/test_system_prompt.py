"""Tests for the composable system prompt builder."""

import datetime
from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.agent.system_prompt import (
    SystemPromptBuilder,
    _strip_integrations_block,
    build_agent_system_prompt,
    build_agent_system_prompt_parts,
    build_date_section,
    build_heartbeat_system_prompt,
    build_identity_section,
    build_instructions_section,
    build_integration_status_section,
    build_memory_section,
    build_proactive_section,
    build_time_user_context,
    build_tool_guidelines_section,
    build_user_section,
    to_local_time,
)
from backend.app.models import User
from backend.app.services.llm_service import prepare_system_with_caching


class TestSystemPromptBuilder:
    def test_empty_builder(self) -> None:
        """Empty builder should produce empty string."""
        builder = SystemPromptBuilder()
        assert builder.build() == ""

    def test_preamble_only(self) -> None:
        """Builder with just preamble should produce it."""
        builder = SystemPromptBuilder()
        builder.set_preamble("Hello world")
        assert builder.build() == "Hello world"

    def test_single_section(self) -> None:
        """Builder with one section should produce heading + content."""
        builder = SystemPromptBuilder()
        builder.add_section("Test", "Content here")
        result = builder.build()
        assert "## Test" in result
        assert "Content here" in result

    def test_preamble_and_sections(self) -> None:
        """Builder should combine preamble and sections with double newlines."""
        builder = SystemPromptBuilder()
        builder.set_preamble("You are a bot.")
        builder.add_section("About", "Details here")
        builder.add_section("Rules", "Be nice")
        result = builder.build()
        assert result.startswith("You are a bot.")
        assert "## About\nDetails here" in result
        assert "## Rules\nBe nice" in result

    def test_empty_content_skipped(self) -> None:
        """Sections with empty content should be omitted."""
        builder = SystemPromptBuilder()
        builder.add_section("Present", "Has content")
        builder.add_section("Empty", "")
        builder.add_section("Also Present", "Also has content")
        result = builder.build()
        assert "## Present" in result
        assert "## Empty" not in result
        assert "## Also Present" in result

    def test_curly_braces_safe(self) -> None:
        """User-supplied content with curly braces should not cause errors."""
        builder = SystemPromptBuilder()
        builder.set_preamble("You are a bot.")
        builder.add_section("About", "User name is {Mike}")
        builder.add_section("Memory", "key={value}")
        result = builder.build()
        assert "{Mike}" in result
        assert "key={value}" in result

    def test_chaining(self) -> None:
        """Builder methods should support method chaining."""
        result = (
            SystemPromptBuilder()
            .set_preamble("Hello")
            .add_section("A", "Content A")
            .add_section("B", "Content B")
            .build()
        )
        assert "Hello" in result
        assert "## A" in result
        assert "## B" in result


class TestBuildParts:
    def test_dynamic_sections_split_into_second_half(self) -> None:
        """build_parts puts stable sections in the first half, dynamic in the second."""
        builder = SystemPromptBuilder()
        builder.set_preamble("Preamble")
        builder.add_section("Stable", "stable content")
        builder.add_section("Dynamic", "dynamic content", dynamic=True)
        stable, dynamic = builder.build_parts()
        assert "Preamble" in stable
        assert "stable content" in stable
        assert "dynamic content" not in stable
        assert "dynamic content" in dynamic
        assert "stable content" not in dynamic

    def test_dynamic_empty_when_all_stable(self) -> None:
        """The dynamic half is empty when no sections are dynamic."""
        builder = SystemPromptBuilder()
        builder.set_preamble("Preamble")
        builder.add_section("A", "content a")
        builder.add_section("B", "content b")
        stable, dynamic = builder.build_parts()
        assert dynamic == ""
        assert "content a" in stable
        assert "content b" in stable

    def test_build_joins_stable_then_dynamic(self) -> None:
        """build() returns stable sections first, then dynamic ones."""
        builder = SystemPromptBuilder()
        builder.set_preamble("Preamble")
        builder.add_section("Instructions", "be helpful")
        builder.add_section("Memory", "user likes coffee", dynamic=True)
        result = builder.build()
        assert result.index("be helpful") < result.index("user likes coffee")

    def test_prepare_system_single_cached_block(self) -> None:
        """prepare_system_with_caching wraps the whole string in one cached block."""
        blocks = prepare_system_with_caching("Just a plain prompt")
        assert len(blocks) == 1
        assert "cache_control" in blocks[0]
        assert blocks[0]["text"] == "Just a plain prompt"

    @pytest.mark.asyncio
    async def test_agent_prompt_parts_split_dynamic_out(self) -> None:
        """build_agent_system_prompt_parts returns memory in the dynamic half only."""
        user = MagicMock()
        user.id = "user-123"
        user.soul_text = "soul"
        user.user_text = "user info"
        user.timezone = ""
        with patch(
            "backend.app.agent.system_prompt.build_memory_context",
            new_callable=AsyncMock,
            return_value="some memory",
        ):
            stable, dynamic = await build_agent_system_prompt_parts(
                user, tools=[], message_context="hello"
            )
        assert "some memory" in dynamic
        assert "some memory" not in stable
        assert "AI assistant for solo tradespeople" in stable


class TestSectionBuilders:
    def test_build_identity_section(self) -> None:
        """Should include soul_text content."""
        user = MagicMock()
        user.soul_text = "I'm Bolt, the AI assistant for Mike."
        result = build_identity_section(user)
        assert "Mike" in result

    @pytest.mark.asyncio
    async def test_build_memory_section_with_content(self) -> None:
        """Should return memory context when available."""
        with patch(
            "backend.app.agent.system_prompt.build_memory_context",
            new_callable=AsyncMock,
            return_value="client: John Doe, deck work",
        ):
            result = await build_memory_section(user_id="1")
        assert "John Doe" in result

    @pytest.mark.asyncio
    async def test_build_memory_section_empty(self) -> None:
        """Should return placeholder when no memories exist."""
        with patch(
            "backend.app.agent.system_prompt.build_memory_context",
            new_callable=AsyncMock,
            return_value="",
        ):
            result = await build_memory_section(user_id="1")
        assert result == "(No memories saved yet)"

    def test_build_instructions_section(self) -> None:
        """Should contain core behavioral rules."""
        result = build_instructions_section()
        assert "only communicate via this chat" in result

    def test_build_instructions_section_no_trade_guidance(self) -> None:
        """Instructions section should not contain trade-specific guidance (removed from model)."""
        result = build_instructions_section()
        assert "Trade guidance" not in result

    def test_build_tool_guidelines_empty(self) -> None:
        """Should return empty string when no tools have usage hints."""
        tool = MagicMock()
        tool.usage_hint = None
        assert build_tool_guidelines_section([tool]) == ""

    def test_build_tool_guidelines_with_hints(self) -> None:
        """Should format tool hints as bullet points."""
        tool1 = MagicMock()
        tool1.usage_hint = "Use save_fact for important info"
        tool2 = MagicMock()
        tool2.usage_hint = "Use create_estimate for quotes"
        result = build_tool_guidelines_section([tool1, tool2])
        assert "- Use save_fact" in result
        assert "- Use create_estimate" in result

    def test_build_proactive_section(self) -> None:
        """Should contain proactive messaging rules."""
        result = build_proactive_section()
        assert "heartbeat" in result
        assert "reminder" in result

    def test_build_proactive_section_explains_outreach(self) -> None:
        """Proactive section should tell the agent it can reach out without user messaging first."""
        result = build_proactive_section()
        assert "proactively" in result
        assert "HEARTBEAT.md" in result


class TestBuildAgentSystemPrompt:
    @pytest.fixture(autouse=True)
    def _stub_integration_status(self) -> Generator[None, None, None]:
        """Default the live integration-status helper to "no integrations".

        The real implementation queries ``oauth_service`` for every
        deployment-configured integration, which would issue a DB query
        against ``oauth_tokens`` for the test user. The tests here don't
        care about that section; suppressing it keeps them focused.
        Tests that want to assert against the section can override this
        fixture via their own ``patch`` inside the test body.
        """
        with patch(
            "backend.app.agent.system_prompt.build_integration_status_section",
            new_callable=AsyncMock,
            return_value="",
        ):
            yield

    @pytest.mark.asyncio
    async def test_assembles_all_sections(self) -> None:
        """Full agent prompt should contain all key sections."""
        user = MagicMock()
        user.soul_text = "I'm Bolt, the AI assistant for Jake."
        user.user_text = ""
        user.id = 1
        user.timezone = ""

        tool = MagicMock()
        tool.usage_hint = "Use save_fact for memories"

        with patch(
            "backend.app.agent.system_prompt.build_memory_context",
            new_callable=AsyncMock,
            return_value="client: Jane, roof repair",
        ):
            result = await build_agent_system_prompt(
                user=user,
                tools=[tool],
                message_context="how much for a roof repair?",
            )

        assert "AI assistant for solo tradespeople" in result
        assert "Jake" in result
        assert "Jane" in result
        assert "Tool Guidelines" in result
        assert "save_fact" in result
        assert "Proactive Messaging" in result

    @pytest.mark.asyncio
    async def test_tool_guidelines_live_in_dynamic_half(self) -> None:
        """Tool guidelines must sit in the dynamic half so that specialist
        activation mid-conversation does not bust the stable system-prompt
        cache. They must never leak into the stable half."""
        user = MagicMock()
        user.soul_text = "I'm Bolt."
        user.user_text = ""
        user.id = 1
        user.timezone = ""

        tool = MagicMock()
        tool.usage_hint = "Use save_fact for memories"

        with patch(
            "backend.app.agent.system_prompt.build_memory_context",
            new_callable=AsyncMock,
            return_value="",
        ):
            stable, dynamic = await build_agent_system_prompt_parts(
                user=user,
                tools=[tool],
                message_context="hello",
            )

        assert "Tool Guidelines" in dynamic
        assert "save_fact" in dynamic
        # The stable half must not leak the tool hints, or activating a new
        # specialist would invalidate the cached prefix.
        assert "Tool Guidelines" not in stable
        assert "save_fact" not in stable

    @pytest.mark.asyncio
    async def test_preamble_is_generic(self) -> None:
        """Agent prompt preamble should be generic (no assistant_name)."""
        user = MagicMock()
        user.soul_text = "I'm Bolt."
        user.user_text = ""
        user.id = 1
        user.timezone = ""

        with patch(
            "backend.app.agent.system_prompt.build_memory_context",
            new_callable=AsyncMock,
            return_value="",
        ):
            result = await build_agent_system_prompt(
                user=user,
                tools=[],
                message_context="hello",
            )

        assert "You are an AI assistant for solo tradespeople" in result

    @pytest.mark.asyncio
    async def test_no_trade_guidance_in_prompt(self) -> None:
        """Agent prompt should not contain trade-specific guidance (removed from model)."""
        user = MagicMock()
        user.soul_text = ""
        user.user_text = ""
        user.id = 1
        user.timezone = ""

        with patch(
            "backend.app.agent.system_prompt.build_memory_context",
            new_callable=AsyncMock,
            return_value="",
        ):
            result = await build_agent_system_prompt(
                user=user,
                tools=[],
                message_context="hello",
            )

        # Trade guidance removed from model; should not appear
        assert "Trade guidance" not in result
        assert "NEC codes" not in result

    @pytest.mark.asyncio
    async def test_curly_braces_in_soul_text(self) -> None:
        """Soul text with curly braces should not break the prompt."""
        user = MagicMock()
        user.soul_text = "I'm the AI for Mike {The Plumber}."
        user.user_text = ""
        user.id = 1
        user.timezone = ""

        with patch(
            "backend.app.agent.system_prompt.build_memory_context",
            new_callable=AsyncMock,
            return_value="",
        ):
            result = await build_agent_system_prompt(
                user=user,
                tools=[],
                message_context="hello",
            )

        assert "Mike {The Plumber}" in result


class TestToLocalTime:
    def test_converts_to_pacific(self) -> None:
        utc = datetime.datetime(2025, 6, 15, 17, 0, tzinfo=datetime.UTC)
        result = to_local_time(utc, "America/Los_Angeles")
        # UTC 17:00 in June (PDT, UTC-7) -> 10:00 local
        assert result.hour == 10

    def test_empty_timezone_returns_utc(self) -> None:
        utc = datetime.datetime(2025, 6, 15, 17, 0, tzinfo=datetime.UTC)
        result = to_local_time(utc, "")
        assert result.hour == 17

    def test_invalid_timezone_returns_utc(self) -> None:
        utc = datetime.datetime(2025, 6, 15, 17, 0, tzinfo=datetime.UTC)
        result = to_local_time(utc, "Not/A_Real_Zone")
        assert result.hour == 17


class TestBuildDateSection:
    @patch("backend.app.agent.system_prompt.datetime")
    def test_includes_day_of_week_and_date(self, mock_dt: MagicMock) -> None:
        mock_dt.UTC = datetime.UTC
        mock_dt.datetime.now.return_value = datetime.datetime(
            2025, 6, 16, 15, 30, tzinfo=datetime.UTC
        )
        user = MagicMock()
        user.timezone = ""
        result = build_date_section(user)
        # 2025-06-16 is a Monday
        assert result == "Monday, 2025-06-16"

    @patch("backend.app.agent.system_prompt.datetime")
    def test_converts_to_local_timezone(self, mock_dt: MagicMock) -> None:
        mock_dt.UTC = datetime.UTC
        # Saturday 3 AM UTC -> Friday 8 PM Pacific (PDT)
        mock_dt.datetime.now.return_value = datetime.datetime(
            2025, 6, 14, 3, 0, tzinfo=datetime.UTC
        )
        user = MagicMock()
        user.timezone = "America/Los_Angeles"
        result = build_date_section(user)
        # Should show Friday (local), not Saturday (UTC)
        assert result == "Friday, 2025-06-13"


class TestAgentSystemPromptExcludesTime:
    @pytest.fixture(autouse=True)
    def _stub_integration_status(self) -> Generator[None, None, None]:
        with patch(
            "backend.app.agent.system_prompt.build_integration_status_section",
            new_callable=AsyncMock,
            return_value="",
        ):
            yield

    @pytest.mark.asyncio
    async def test_agent_prompt_does_not_include_time(self) -> None:
        """System prompt should NOT include current time (moved to user message for caching)."""
        user = MagicMock()
        user.soul_text = ""
        user.user_text = ""
        user.timezone = "America/Los_Angeles"
        user.id = 1

        with patch(
            "backend.app.agent.system_prompt.build_memory_context",
            new_callable=AsyncMock,
            return_value="",
        ):
            result = await build_agent_system_prompt(
                user=user,
                tools=[],
                message_context="hello",
            )

        assert "## Current date and time" not in result
        assert "## Current time" not in result


class TestBuildTimeUserContext:
    @patch("backend.app.agent.system_prompt.datetime")
    def test_includes_time_with_iana_timezone(self, mock_dt: MagicMock) -> None:
        """Should produce a bracketed time string ending with the IANA timezone.

        The timezone name anchors relative-time math: without it, the LLM
        has been observed treating local times as UTC and producing wrong
        deltas (e.g. saying 7:30am is "minutes from now" when local was
        6:27am). Issue #1067.
        """
        mock_dt.UTC = datetime.UTC
        mock_dt.datetime.now.return_value = datetime.datetime(
            2025, 6, 15, 17, 30, tzinfo=datetime.UTC
        )
        user = MagicMock()
        user.timezone = "America/New_York"
        result = build_time_user_context(user)
        assert result == "[Current time: Sunday, 2025-06-15 01:30 PM (America/New_York)]"

    @patch("backend.app.agent.system_prompt.datetime")
    def test_utc_fallback_when_no_timezone(self, mock_dt: MagicMock) -> None:
        """Should fall back to UTC and prompt for timezone discovery."""
        mock_dt.UTC = datetime.UTC
        mock_dt.datetime.now.return_value = datetime.datetime(
            2025, 6, 15, 17, 30, tzinfo=datetime.UTC
        )
        user = MagicMock()
        user.timezone = ""
        result = build_time_user_context(user)
        assert "[Current time:" in result
        assert "(UTC)" in result
        assert "No timezone has been configured yet" in result


# ---------------------------------------------------------------------------
# Live integration status section (issue: stale USER.md integration block)
# ---------------------------------------------------------------------------


class TestStripIntegrationsBlock:
    def test_strips_h1_integrations_section(self) -> None:
        """A leading-hash ``# Integrations`` block should be removed."""
        text = (
            "# User Profile\n"
            "Name: Alice\n"
            "\n"
            "# Integrations\n"
            "- Google Calendar: connected\n"
            "- QuickBooks Online: connected\n"
        )
        result = _strip_integrations_block(text)
        assert "# Integrations" not in result
        assert "Google Calendar" not in result
        assert "Name: Alice" in result

    def test_strips_h2_integrations_section(self) -> None:
        """A ``## Integrations`` subsection should be removed."""
        text = (
            "## Profile\n"
            "Name: Alice\n"
            "\n"
            "## Integrations\n"
            "- Google Drive: connected\n"
            "\n"
            "## Other\n"
            "Other content\n"
        )
        result = _strip_integrations_block(text)
        assert "## Integrations" not in result
        assert "Google Drive" not in result
        # Sibling section after the stripped block is preserved.
        assert "## Other" in result
        assert "Other content" in result

    def test_no_op_when_no_integrations_block(self) -> None:
        """Text without an Integrations heading should pass through unchanged."""
        text = "## Profile\nName: Alice\n"
        assert _strip_integrations_block(text) == text

    def test_no_op_when_empty(self) -> None:
        """Empty / None-equivalent input should return empty string."""
        assert _strip_integrations_block("") == ""

    def test_does_not_touch_word_integrations_in_prose(self) -> None:
        """The literal word ``integrations`` in prose must not trigger a strip."""
        text = "## Profile\nUses several integrations daily.\n"
        result = _strip_integrations_block(text)
        assert "Uses several integrations daily" in result

    def test_stops_at_same_depth_heading(self) -> None:
        """The strip ends at the next heading of equal or shallower depth."""
        text = (
            "## Integrations\n"
            "- A: connected\n"
            "### A subheading nested inside\n"
            "should also be stripped\n"
            "## Next Section\n"
            "kept\n"
        )
        result = _strip_integrations_block(text)
        assert "- A: connected" not in result
        assert "should also be stripped" not in result
        assert "## Next Section" in result
        assert "kept" in result


class TestBuildUserSectionStripsIntegrations:
    def test_user_section_strips_legacy_integrations_block(self) -> None:
        """``build_user_section`` should defensively strip a stale Integrations block."""
        user = MagicMock(spec=User)
        user.user_text = "# User Profile\nName: Bob\n\n# Integrations\n- Google Drive: connected\n"
        result = build_user_section(user)
        assert "Name: Bob" in result
        assert "Google Drive" not in result


class TestBuildIntegrationStatusSection:
    @pytest.mark.asyncio
    async def test_renders_connected_and_not_connected(self) -> None:
        with patch(
            "backend.app.agent.tools.integration_tools.get_user_connected_integrations",
            new_callable=AsyncMock,
            return_value={
                "google_drive": False,
                "google_calendar": True,
                "quickbooks": True,
                "appfolio_vendor": False,
            },
        ):
            result = await build_integration_status_section("user-123")
        assert "Connected: google_calendar, quickbooks" in result
        assert "Not connected: appfolio_vendor, google_drive" in result
        assert "Authoritative" in result

    @pytest.mark.asyncio
    async def test_empty_when_no_integrations_configured(self) -> None:
        """No section content when the deployment has zero integrations wired."""
        with patch(
            "backend.app.agent.tools.integration_tools.get_user_connected_integrations",
            new_callable=AsyncMock,
            return_value={},
        ):
            result = await build_integration_status_section("user-123")
        assert result == ""

    @pytest.mark.asyncio
    async def test_renders_all_not_connected(self) -> None:
        """If nothing is connected, the section explicitly says so."""
        with patch(
            "backend.app.agent.tools.integration_tools.get_user_connected_integrations",
            new_callable=AsyncMock,
            return_value={"google_drive": False, "quickbooks": False},
        ):
            result = await build_integration_status_section("user-123")
        assert "Connected: (none)" in result
        assert "Not connected: google_drive, quickbooks" in result


class TestAgentPromptIncludesLiveIntegrationStatus:
    @pytest.mark.asyncio
    async def test_section_lands_in_dynamic_half(self) -> None:
        """The live integration status sits in the dynamic half.

        Mid-conversation OAuth completions must take effect on the very
        next turn, which means the section cannot be inside the cached
        stable prefix. Placing it ``dynamic=True`` keeps it in the dynamic
        half that is appended to the user turn.
        """
        user = MagicMock()
        user.soul_text = "soul"
        user.user_text = "user"
        user.id = 1
        user.timezone = ""

        with (
            patch(
                "backend.app.agent.system_prompt.build_memory_context",
                new_callable=AsyncMock,
                return_value="",
            ),
            patch(
                "backend.app.agent.system_prompt.build_integration_status_section",
                new_callable=AsyncMock,
                return_value=(
                    "Live connection state. Authoritative over anything in USER.md "
                    "or MEMORY.md.\nConnected: google_calendar\nNot connected: google_drive"
                ),
            ),
        ):
            stable, dynamic = await build_agent_system_prompt_parts(
                user=user,
                tools=[],
                message_context="hello",
            )

        assert "## Connected Integrations" in dynamic
        assert "Connected: google_calendar" in dynamic
        assert "Not connected: google_drive" in dynamic
        assert "## Connected Integrations" not in stable

    @pytest.mark.asyncio
    async def test_heartbeat_prompt_includes_section(self) -> None:
        """The heartbeat-decision prompt also gets the live integration section.

        Heartbeat runs on a timer outside the agent loop and was previously
        the only place where a stale USER.md integration block would still
        bite (the agent had no chance to ``manage_integration(status)``
        between heartbeats). Live injection closes that gap.
        """
        user = MagicMock()
        user.soul_text = "soul"
        user.user_text = "user"
        user.id = 1
        user.timezone = ""

        with (
            patch(
                "backend.app.agent.system_prompt.build_memory_context",
                new_callable=AsyncMock,
                return_value="",
            ),
            patch(
                "backend.app.agent.system_prompt.build_integration_status_section",
                new_callable=AsyncMock,
                return_value="Connected: google_calendar\nNot connected: google_drive",
            ),
        ):
            prompt = await build_heartbeat_system_prompt(user, recent_messages="(none)")

        assert "## Connected Integrations" in prompt
        assert "Connected: google_calendar" in prompt
        assert "Not connected: google_drive" in prompt

    @pytest.mark.asyncio
    async def test_heartbeat_prompt_omits_section_when_no_integrations(self) -> None:
        """When the deployment has no integrations configured, the section
        is suppressed entirely rather than rendering an empty heading."""
        user = MagicMock()
        user.soul_text = "soul"
        user.user_text = "user"
        user.id = 1
        user.timezone = ""

        with (
            patch(
                "backend.app.agent.system_prompt.build_memory_context",
                new_callable=AsyncMock,
                return_value="",
            ),
            patch(
                "backend.app.agent.system_prompt.build_integration_status_section",
                new_callable=AsyncMock,
                return_value="",
            ),
        ):
            prompt = await build_heartbeat_system_prompt(user, recent_messages="(none)")

        assert "## Connected Integrations" not in prompt
