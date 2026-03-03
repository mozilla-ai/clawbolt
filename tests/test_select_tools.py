"""Tests for context-aware tool selection via select_tools()."""

import pytest

from backend.app.agent.tools.registry import select_tools

# The full set of known factory names used in tests.
ALL_FACTORIES = ["memory", "messaging", "estimate", "checklist", "profile", "file"]


class TestAlwaysIncludedTools:
    """Memory, messaging, and profile tools are always included."""

    def test_always_includes_memory(self) -> None:
        result = select_tools("how much for a deck?", factory_names=ALL_FACTORIES)
        assert "memory" in result

    def test_always_includes_messaging(self) -> None:
        result = select_tools("how much for a deck?", factory_names=ALL_FACTORIES)
        assert "messaging" in result

    def test_always_includes_profile(self) -> None:
        result = select_tools("how much for a deck?", factory_names=ALL_FACTORIES)
        assert "profile" in result


class TestEstimateKeywords:
    """Estimate tools are included when pricing keywords appear."""

    @pytest.mark.parametrize(
        "keyword",
        ["estimate", "quote", "bid", "price", "cost", "how much", "invoice"],
    )
    def test_estimate_keyword_triggers_inclusion(self, keyword: str) -> None:
        result = select_tools(
            f"Can you give me a {keyword} for the bathroom remodel?",
            factory_names=ALL_FACTORIES,
        )
        assert "estimate" in result

    def test_estimate_keyword_case_insensitive(self) -> None:
        result = select_tools("Give me an ESTIMATE", factory_names=ALL_FACTORIES)
        assert "estimate" in result

    def test_estimate_keyword_does_not_include_checklist(self) -> None:
        result = select_tools("How much for a deck?", factory_names=ALL_FACTORIES)
        assert "estimate" in result
        assert "checklist" not in result


class TestChecklistKeywords:
    """Checklist tools are included when task keywords appear."""

    @pytest.mark.parametrize(
        "keyword",
        ["checklist", "reminder", "todo", "task", "to-do"],
    )
    def test_checklist_keyword_triggers_inclusion(self, keyword: str) -> None:
        result = select_tools(
            f"Add a {keyword} for the Johnson project",
            factory_names=ALL_FACTORIES,
        )
        assert "checklist" in result

    def test_checklist_keyword_case_insensitive(self) -> None:
        result = select_tools("Show my CHECKLIST", factory_names=ALL_FACTORIES)
        assert "checklist" in result

    def test_checklist_keyword_does_not_include_estimate(self) -> None:
        result = select_tools("Add a task for tomorrow", factory_names=ALL_FACTORIES)
        assert "checklist" in result
        assert "estimate" not in result


class TestFileTools:
    """File tools are included only when media is present AND storage is configured."""

    def test_file_included_with_media_and_storage(self) -> None:
        result = select_tools(
            "Here is the photo",
            has_media=True,
            has_storage=True,
            factory_names=ALL_FACTORIES,
        )
        assert "file" in result

    def test_file_excluded_without_media(self) -> None:
        """No media means file tools are not specifically selected."""
        result = select_tools(
            "How much for a deck?",
            has_media=False,
            has_storage=True,
            factory_names=ALL_FACTORIES,
        )
        # estimate keyword matched, so only specialized + always tools
        assert "file" not in result

    def test_file_excluded_without_storage(self) -> None:
        """No storage means file tools are not specifically selected."""
        result = select_tools(
            "How much for a deck?",
            has_media=True,
            has_storage=False,
            factory_names=ALL_FACTORIES,
        )
        # estimate keyword matched, so only specialized + always tools
        assert "file" not in result


class TestFallbackBehavior:
    """When no specialized keywords match, all tools are included."""

    def test_generic_message_includes_all_tools(self) -> None:
        result = select_tools("Hello, how are you?", factory_names=ALL_FACTORIES)
        assert result == set(ALL_FACTORIES)

    def test_empty_message_includes_all_tools(self) -> None:
        result = select_tools("", factory_names=ALL_FACTORIES)
        assert result == set(ALL_FACTORIES)

    def test_ambiguous_message_includes_all_tools(self) -> None:
        result = select_tools(
            "Can you help me with my project?",
            factory_names=ALL_FACTORIES,
        )
        assert result == set(ALL_FACTORIES)


class TestMultipleKeywords:
    """Messages matching multiple keyword groups include all matched tools."""

    def test_estimate_and_checklist_both_included(self) -> None:
        result = select_tools(
            "Give me a quote and add a reminder to follow up",
            factory_names=ALL_FACTORIES,
        )
        assert "estimate" in result
        assert "checklist" in result
        assert "memory" in result
        assert "messaging" in result
        assert "profile" in result
        # file not matched (no media), and specialized matched, so file excluded
        assert "file" not in result

    def test_estimate_with_media_and_storage(self) -> None:
        result = select_tools(
            "How much would this cost?",
            has_media=True,
            has_storage=True,
            factory_names=ALL_FACTORIES,
        )
        assert "estimate" in result
        assert "file" in result
        assert "memory" in result


class TestFactoryNamesParameter:
    """select_tools respects the factory_names parameter."""

    def test_limits_to_provided_factory_names(self) -> None:
        result = select_tools(
            "Hello there",
            factory_names=["memory", "messaging"],
        )
        assert result == {"memory", "messaging"}

    def test_unknown_factory_in_keyword_rule_ignored(self) -> None:
        """If estimate is not in factory_names, it cannot be selected."""
        result = select_tools(
            "How much for a deck?",
            factory_names=["memory", "messaging", "profile"],
        )
        # No specialized keyword matched for available factories, fallback to all
        assert result == {"memory", "messaging", "profile"}

    def test_none_factory_names_uses_defaults(self) -> None:
        result = select_tools("Hello there", factory_names=None)
        # Should use the hardcoded default set and include all (fallback)
        assert "memory" in result
        assert "messaging" in result


class TestWordBoundaries:
    """Keywords must match at word boundaries to avoid false positives."""

    def test_estimated_does_not_match_estimate(self) -> None:
        """'estimated' contains 'estimate' but should still match at word boundary."""
        # Actually 'estimated' does start with 'estimate' at a word boundary,
        # but the regex uses \b which checks boundaries. 'estimated' has
        # 'estimate' followed by 'd', so \bestimate\b won't match 'estimated'.
        result = select_tools("The estimated time is 3 hours", factory_names=ALL_FACTORIES)
        # 'estimated' does NOT match \bestimate\b, so no specialized match, fallback
        assert result == set(ALL_FACTORIES)

    def test_costing_matches_estimate(self) -> None:
        result = select_tools("The costing method is simple", factory_names=ALL_FACTORIES)
        # 'costing' matches the estimate regex as a verb form
        assert "estimate" in result

    def test_priceless_does_not_match_price(self) -> None:
        result = select_tools("That view is priceless", factory_names=ALL_FACTORIES)
        # 'priceless' does NOT match \bprice\b, fallback to all
        assert result == set(ALL_FACTORIES)


class TestPluralForms:
    """Plural and verb forms of keywords trigger tool selection."""

    @pytest.mark.parametrize(
        "keyword",
        ["estimates", "quotes", "bids", "prices", "pricing", "costs", "costing", "invoices"],
    )
    def test_estimate_plural_and_verb_forms(self, keyword: str) -> None:
        result = select_tools(
            f"Send me the {keyword} for this job",
            factory_names=ALL_FACTORIES,
        )
        assert "estimate" in result

    @pytest.mark.parametrize(
        "keyword",
        ["checklists", "reminders", "todos", "tasks", "to-dos"],
    )
    def test_checklist_plural_forms(self, keyword: str) -> None:
        result = select_tools(
            f"Show me my {keyword}",
            factory_names=ALL_FACTORIES,
        )
        assert "checklist" in result


class TestMediaOrthogonality:
    """Media presence adds file tools but does not suppress fallback."""

    def test_media_without_keywords_still_falls_back_to_all(self) -> None:
        """When media is present but no keywords match, all tools are included."""
        result = select_tools(
            "Here is the photo",
            has_media=True,
            has_storage=True,
            factory_names=ALL_FACTORIES,
        )
        # No keyword matched, so fallback includes all tools
        assert result == set(ALL_FACTORIES)

    def test_media_with_keywords_does_not_include_unmatched(self) -> None:
        """When media is present and keywords match, only matched + file + always tools."""
        result = select_tools(
            "Here is the estimate photo",
            has_media=True,
            has_storage=True,
            factory_names=ALL_FACTORIES,
        )
        assert "estimate" in result
        assert "file" in result
        assert "memory" in result
        assert "checklist" not in result
