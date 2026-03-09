"""Tests for the composable system prompt builder."""

import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.agent.file_store import UserData
from backend.app.agent.system_prompt import (
    SystemPromptBuilder,
    _to_user_time,
    build_agent_system_prompt,
    build_cross_session_context,
    build_date_section,
    build_identity_section,
    build_instructions_section,
    build_local_datetime_section,
    build_memory_section,
    build_proactive_section,
    build_recall_section,
    build_tool_guidelines_section,
)


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


class TestSectionBuilders:
    def test_build_identity_section(self) -> None:
        """Should include user name."""
        user = MagicMock()
        user.name = "Mike"
        user.soul_text = None
        user.preferences_json = None
        user.assistant_name = "Clawbolt"
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
            result = await build_memory_section(user_id=1)
        assert "John Doe" in result

    @pytest.mark.asyncio
    async def test_build_memory_section_empty(self) -> None:
        """Should return placeholder when no memories exist."""
        with patch(
            "backend.app.agent.system_prompt.build_memory_context",
            new_callable=AsyncMock,
            return_value="",
        ):
            result = await build_memory_section(user_id=1)
        assert result == "(No memories saved yet)"

    def test_build_instructions_section(self) -> None:
        """Should contain core behavioral rules."""
        result = build_instructions_section()
        assert "concise" in result
        assert "ONLY communicate via this chat" in result

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
        assert "checklist" in result
        assert "reminder" in result

    def test_build_recall_section(self) -> None:
        """Should contain recall behavior rules."""
        result = build_recall_section()
        assert "Search your memory" in result
        assert "don't make things up" in result


class TestBuildAgentSystemPrompt:
    @pytest.mark.asyncio
    async def test_assembles_all_sections(self) -> None:
        """Full agent prompt should contain all key sections."""
        user = MagicMock()
        user.name = "Jake"
        user.soul_text = None
        user.preferences_json = None
        user.assistant_name = "Clawbolt"
        user.id = 1

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

        assert "Clawbolt" in result
        assert "Jake" in result
        assert "Jane" in result
        assert "Tool Guidelines" in result
        assert "save_fact" in result
        assert "Proactive Messaging" in result
        assert "Recall Behavior" in result

    @pytest.mark.asyncio
    async def test_preamble_uses_assistant_name(self) -> None:
        """Agent prompt preamble should use custom assistant_name."""
        user = MagicMock()
        user.name = "Jake"
        user.soul_text = None
        user.preferences_json = None
        user.assistant_name = "Bolt"
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

        assert "You are Bolt, an AI assistant" in result
        assert "Clawbolt" not in result.split("\n")[0]

    @pytest.mark.asyncio
    async def test_no_trade_guidance_in_prompt(self) -> None:
        """Agent prompt should not contain trade-specific guidance (removed from model)."""
        user = MagicMock()
        user.name = "Sparky"
        user.soul_text = None
        user.preferences_json = None
        user.assistant_name = "Clawbolt"
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

        # Trade guidance removed from model; should not appear
        assert "Trade guidance" not in result
        assert "NEC codes" not in result

    @pytest.mark.asyncio
    async def test_curly_braces_in_owner_name(self) -> None:
        """User name with curly braces should not break the prompt."""
        user = MagicMock()
        user.name = "Mike {The Plumber}"
        user.soul_text = None
        user.preferences_json = None
        user.assistant_name = "Clawbolt"
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

        assert "Mike {The Plumber}" in result


class TestToUserTime:
    def test_converts_to_pacific(self) -> None:
        utc = datetime.datetime(2025, 6, 15, 17, 0, tzinfo=datetime.UTC)
        result = _to_user_time(utc, "America/Los_Angeles")
        # UTC 17:00 in June (PDT, UTC-7) -> 10:00 local
        assert result.hour == 10

    def test_empty_timezone_returns_utc(self) -> None:
        utc = datetime.datetime(2025, 6, 15, 17, 0, tzinfo=datetime.UTC)
        result = _to_user_time(utc, "")
        assert result.hour == 17

    def test_invalid_timezone_returns_utc(self) -> None:
        utc = datetime.datetime(2025, 6, 15, 17, 0, tzinfo=datetime.UTC)
        result = _to_user_time(utc, "Not/A_Real_Zone")
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
    def test_converts_to_user_timezone(self, mock_dt: MagicMock) -> None:
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


class TestBuildLocalDatetimeSection:
    @patch("backend.app.agent.system_prompt.datetime")
    def test_includes_time_and_timezone(self, mock_dt: MagicMock) -> None:
        mock_dt.UTC = datetime.UTC
        mock_dt.datetime.now.return_value = datetime.datetime(
            2025, 6, 15, 17, 30, tzinfo=datetime.UTC
        )
        user = MagicMock()
        user.timezone = "America/New_York"
        result = build_local_datetime_section(user)
        # UTC 17:30 -> 1:30 PM EDT
        assert "01:30 PM" in result
        assert "Sunday" in result
        assert "2025-06-15" in result

    @patch("backend.app.agent.system_prompt.datetime")
    def test_utc_fallback_when_no_timezone(self, mock_dt: MagicMock) -> None:
        mock_dt.UTC = datetime.UTC
        mock_dt.datetime.now.return_value = datetime.datetime(
            2025, 6, 15, 17, 30, tzinfo=datetime.UTC
        )
        user = MagicMock()
        user.timezone = ""
        result = build_local_datetime_section(user)
        assert "05:30 PM" in result
        assert "Sunday" in result


class TestAgentSystemPromptIncludesDate:
    @pytest.mark.asyncio
    @patch("backend.app.agent.system_prompt.datetime")
    async def test_agent_prompt_has_current_date(self, mock_dt: MagicMock) -> None:
        """Main agent prompt should include a Current date section."""
        mock_dt.UTC = datetime.UTC
        mock_dt.datetime.now.return_value = datetime.datetime(
            2025, 6, 16, 15, 0, tzinfo=datetime.UTC
        )
        user = MagicMock()
        user.name = "Jake"
        user.soul_text = None
        user.preferences_json = None
        user.assistant_name = "Clawbolt"
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

        assert "## Current date" in result
        assert "Monday" in result
        assert "2025-06-16" in result


class TestCrossSessionContext:
    def test_returns_empty_when_no_other_sessions(
        self,
        test_user: "UserData",
    ) -> None:
        """Should return empty string when no other sessions exist."""
        result = build_cross_session_context(test_user.id, current_session_id="nonexistent_999")
        assert result == ""

    @pytest.mark.asyncio()
    async def test_includes_messages_from_other_session(
        self,
        test_user: "UserData",
    ) -> None:
        """Should include messages from sessions other than the current one."""
        from backend.app.agent.file_store import get_session_store

        store = get_session_store(test_user.id)

        # Create session A with messages
        session_a, _ = await store.get_or_create_session()
        await store.add_message(session_a, "inbound", "Hello from Telegram")
        await store.add_message(session_a, "outbound", "Hi! How can I help?")

        result = build_cross_session_context(
            test_user.id, current_session_id="different_session_999"
        )
        assert "Hello from Telegram" in result
        assert "Hi! How can I help?" in result
        assert "[User]" in result
        assert "[You]" in result

    @pytest.mark.asyncio()
    async def test_excludes_current_session(
        self,
        test_user: "UserData",
    ) -> None:
        """Should not include messages from the current session."""
        from backend.app.agent.file_store import get_session_store

        store = get_session_store(test_user.id)

        session_a, _ = await store.get_or_create_session()
        await store.add_message(session_a, "inbound", "Message in session A")

        # When querying with session A's own ID, nothing should appear
        result = build_cross_session_context(test_user.id, current_session_id=session_a.session_id)
        assert result == ""

    @pytest.mark.asyncio()
    async def test_truncates_long_messages(
        self,
        test_user: "UserData",
    ) -> None:
        """Long message bodies should be truncated."""
        from backend.app.agent.file_store import get_session_store

        store = get_session_store(test_user.id)
        session_a, _ = await store.get_or_create_session()
        long_body = "x" * 300
        await store.add_message(session_a, "inbound", long_body)

        result = build_cross_session_context(test_user.id, current_session_id="other_999")
        assert "..." in result
        # Should be truncated to ~200 chars + "..."
        assert "x" * 201 not in result

    @pytest.mark.asyncio()
    async def test_agent_prompt_includes_cross_session_context(
        self,
        test_user: "UserData",
    ) -> None:
        """Agent system prompt should include cross-session context when available."""
        from backend.app.agent.file_store import get_session_store

        store = get_session_store(test_user.id)

        # Create a session with messages (simulates a Telegram conversation)
        session_a, _ = await store.get_or_create_session()
        await store.add_message(session_a, "inbound", "Draft estimate for deck")
        await store.add_message(session_a, "outbound", "Sure, what size deck?")

        user = MagicMock()
        user.name = "Jake"
        user.soul_text = None
        user.preferences_json = None
        user.assistant_name = "Clawbolt"
        user.id = test_user.id
        user.user_text = ""
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
                current_session_id="webchat_session_999",
            )

        assert "## Recent Activity (other channel)" in result
        assert "Draft estimate for deck" in result
        assert "Sure, what size deck?" in result
