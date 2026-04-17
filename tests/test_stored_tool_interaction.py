"""Tests for StoredToolInteraction schema validation."""

import json
import logging

import pytest

from backend.app.agent.context import (
    StoredToolInteraction,
    _expand_outbound_with_tools,
    _parse_tool_interactions,
)
from backend.app.agent.messages import AssistantMessage, ToolResultMessage


class TestStoredToolInteractionModel:
    """Tests for the StoredToolInteraction Pydantic model."""

    def test_full_record_parses(self) -> None:
        """A complete record with all fields should parse correctly."""
        data = {
            "tool_call_id": "call_abc123",
            "name": "save_memory",
            "args": {"key": "trade", "value": "electrician"},
            "result": "Saved.",
            "is_error": False,
        }
        model = StoredToolInteraction.model_validate(data)
        assert model.tool_call_id == "call_abc123"
        assert model.name == "save_memory"
        assert model.args == {"key": "trade", "value": "electrician"}
        assert model.result == "Saved."
        assert model.is_error is False

    def test_missing_fields_get_defaults(self) -> None:
        """Missing fields should receive default values."""
        model = StoredToolInteraction.model_validate({})
        assert model.tool_call_id == ""
        assert model.name == ""
        assert model.args == {}
        assert model.result == ""
        assert model.is_error is False

    def test_partial_record_gets_defaults(self) -> None:
        """A partial record (some fields present) gets defaults for the rest."""
        data = {"name": "create_estimate", "is_error": True}
        model = StoredToolInteraction.model_validate(data)
        assert model.name == "create_estimate"
        assert model.is_error is True
        assert model.tool_call_id == ""
        assert model.args == {}
        assert model.result == ""

    def test_extra_fields_ignored(self) -> None:
        """Unknown extra fields should not cause validation failure."""
        data = {
            "tool_call_id": "call_xyz",
            "name": "lookup_client",
            "args": {},
            "result": "Found client.",
            "is_error": False,
            "unknown_field": "ignored",
        }
        model = StoredToolInteraction.model_validate(data)
        assert model.name == "lookup_client"

    def test_tags_field_defaults_empty(self) -> None:
        """tags should default to an empty set."""
        model = StoredToolInteraction.model_validate({})
        assert model.tags == set()

    def test_tags_excluded_from_model_dump(self) -> None:
        """model_dump should exclude tags (Field(exclude=True))."""
        model = StoredToolInteraction(
            tool_call_id="call_1",
            name="tool_a",
            args={"x": 1},
            result="ok",
            is_error=False,
            tags={"SAVES_MEMORY"},
        )
        dumped = model.model_dump()
        assert set(dumped.keys()) == {
            "tool_call_id",
            "name",
            "args",
            "result",
            "is_error",
            "receipt",
        }
        assert "tags" not in dumped

    def test_round_trip_json(self) -> None:
        """Serializing to JSON and parsing back should produce the same model."""
        original = StoredToolInteraction(
            tool_call_id="call_rt",
            name="send_estimate",
            args={"estimate_id": 42},
            result="Estimate sent successfully.",
            is_error=False,
        )
        json_str = json.dumps([original.model_dump()])
        parsed = json.loads(json_str)
        restored = StoredToolInteraction.model_validate(parsed[0])
        assert restored == original


class TestParseToolInteractions:
    """Tests for _parse_tool_interactions with schema validation."""

    def test_valid_json_list(self) -> None:
        """Valid JSON list of tool interaction dicts should be parsed."""
        records = [
            {
                "tool_call_id": "call_1",
                "name": "save_memory",
                "args": {"key": "name", "value": "Joe"},
                "result": "Saved.",
                "is_error": False,
            }
        ]
        raw = json.dumps(records)
        result = _parse_tool_interactions(raw)
        assert len(result) == 1
        assert isinstance(result[0], StoredToolInteraction)
        assert result[0].name == "save_memory"

    def test_empty_string_returns_empty(self) -> None:
        """Empty string input should return empty list."""
        assert _parse_tool_interactions("") == []

    def test_none_returns_empty(self) -> None:
        """None input should return empty list."""
        assert _parse_tool_interactions(None) == []  # type: ignore[arg-type]

    def test_invalid_json_returns_empty(self) -> None:
        """Malformed JSON should return empty list."""
        assert _parse_tool_interactions("{broken json") == []

    def test_non_list_json_returns_empty(self) -> None:
        """JSON that is not a list should return empty list."""
        assert _parse_tool_interactions('{"not": "a list"}') == []

    def test_missing_fields_get_defaults(self) -> None:
        """Records with missing fields should parse with defaults."""
        records = [{"name": "some_tool"}]
        raw = json.dumps(records)
        result = _parse_tool_interactions(raw)
        assert len(result) == 1
        assert result[0].tool_call_id == ""
        assert result[0].args == {}
        assert result[0].result == ""
        assert result[0].is_error is False

    def test_invalid_record_skipped_with_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """Completely invalid records (non-dict) should be skipped with a warning."""
        records = [
            {"name": "good_tool", "tool_call_id": "call_1"},
            "not a dict",
            42,
            {"name": "another_good_tool", "tool_call_id": "call_2"},
        ]
        raw = json.dumps(records)
        with caplog.at_level(logging.WARNING):
            result = _parse_tool_interactions(raw)

        assert len(result) == 2
        assert result[0].name == "good_tool"
        assert result[1].name == "another_good_tool"
        # Check that warnings were logged for the bad records
        warning_messages = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warning_messages) == 2
        assert "index 1" in warning_messages[0]
        assert "index 2" in warning_messages[1]

    def test_mixed_valid_and_invalid(self) -> None:
        """A mix of valid and invalid records preserves valid ones."""
        records = [
            {"tool_call_id": "call_a", "name": "tool_a"},
            None,
            {"tool_call_id": "call_c", "name": "tool_c"},
        ]
        raw = json.dumps(records)
        result = _parse_tool_interactions(raw)
        assert len(result) == 2
        assert result[0].tool_call_id == "call_a"
        assert result[1].tool_call_id == "call_c"


class TestExpandOutboundWithTools:
    """Tests for _expand_outbound_with_tools with StoredToolInteraction models."""

    def test_expand_produces_correct_message_sequence(self) -> None:
        """Expansion should produce assistant(tool_calls) + tool_results + assistant(reply)."""
        interactions = [
            StoredToolInteraction(
                tool_call_id="call_1",
                name="save_memory",
                args={"key": "name", "value": "Alice"},
                result="Saved.",
                is_error=False,
            ),
        ]
        messages = _expand_outbound_with_tools(interactions, "Got it, Alice!")
        assert len(messages) == 3
        # First: assistant with tool_calls
        assert isinstance(messages[0], AssistantMessage)
        assert len(messages[0].tool_calls) == 1
        assert messages[0].tool_calls[0].name == "save_memory"
        assert messages[0].tool_calls[0].id == "call_1"
        assert messages[0].tool_calls[0].arguments == {"key": "name", "value": "Alice"}
        # Second: tool result
        assert isinstance(messages[1], ToolResultMessage)
        assert messages[1].tool_call_id == "call_1"
        assert messages[1].content == "Saved."
        # Third: final assistant reply
        assert isinstance(messages[2], AssistantMessage)
        assert messages[2].content == "Got it, Alice!"

    def test_expand_multiple_tools(self) -> None:
        """Multiple tool interactions should produce multiple results."""
        interactions = [
            StoredToolInteraction(
                tool_call_id="call_1",
                name="tool_a",
                args={},
                result="result_a",
            ),
            StoredToolInteraction(
                tool_call_id="call_2",
                name="tool_b",
                args={"x": 1},
                result="result_b",
            ),
        ]
        messages = _expand_outbound_with_tools(interactions, "Done.")
        # 1 assistant(tool_calls) + 2 tool_results + 1 assistant(reply) = 4
        assert len(messages) == 4
        assert isinstance(messages[0], AssistantMessage)
        assert len(messages[0].tool_calls) == 2
        assert isinstance(messages[1], ToolResultMessage)
        assert isinstance(messages[2], ToolResultMessage)
        assert isinstance(messages[3], AssistantMessage)


class TestPersistOutboundValidation:
    """Tests for persist_outbound using StoredToolInteraction serialization."""

    def test_serialization_excludes_tags(self) -> None:
        """model_dump() should exclude tags via Field(exclude=True)."""
        tc = StoredToolInteraction(
            tool_call_id="call_1",
            name="save_memory",
            args={"key": "k"},
            result="ok",
            is_error=False,
            tags={"SAVES_MEMORY"},
        )
        json_str = json.dumps([tc.model_dump()])
        parsed = json.loads(json_str)
        assert len(parsed) == 1
        assert "tags" not in parsed[0]
        assert parsed[0]["name"] == "save_memory"

    def test_serialization_round_trip(self) -> None:
        """Tool interactions should survive serialize -> parse round trip."""
        tc = StoredToolInteraction(
            tool_call_id="call_rt",
            name="create_estimate",
            args={"client_name": "Bob", "total": 500},
            result="Estimate #1 created.",
            is_error=False,
            tags={"SENDS_REPLY"},
        )
        # Serialize (as persist_outbound does)
        json_str = json.dumps([tc.model_dump()])

        # Parse back (as _parse_tool_interactions does)
        restored = _parse_tool_interactions(json_str)
        assert len(restored) == 1
        assert restored[0].tool_call_id == "call_rt"
        assert restored[0].name == "create_estimate"
        assert restored[0].args == {"client_name": "Bob", "total": 500}
        assert restored[0].result == "Estimate #1 created."
        assert restored[0].is_error is False
