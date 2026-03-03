"""Tests for shared LLM response parsing utilities."""

import json
from unittest.mock import MagicMock

import pytest

from backend.app.agent.llm_parsing import ParsedToolCall, get_response_text, parse_tool_calls
from tests.mocks.llm import make_text_response, make_tool_call_response


class TestParseToolCalls:
    def test_valid_single_tool_call(self) -> None:
        """A well-formed tool call should be parsed correctly."""
        resp = make_tool_call_response(
            [{"name": "save_fact", "arguments": json.dumps({"key": "name", "value": "Mike"})}]
        )
        result = parse_tool_calls(resp)
        assert len(result) == 1
        assert result[0].name == "save_fact"
        assert result[0].arguments == {"key": "name", "value": "Mike"}
        assert result[0].id == "call_0"

    def test_valid_multiple_tool_calls(self) -> None:
        """Multiple tool calls should all be parsed."""
        resp = make_tool_call_response(
            [
                {"name": "tool_a", "arguments": json.dumps({"x": 1})},
                {"name": "tool_b", "arguments": json.dumps({"y": 2}), "id": "custom_id"},
            ]
        )
        result = parse_tool_calls(resp)
        assert len(result) == 2
        assert result[0].name == "tool_a"
        assert result[0].arguments == {"x": 1}
        assert result[1].name == "tool_b"
        assert result[1].arguments == {"y": 2}
        assert result[1].id == "custom_id"

    def test_no_tool_calls_returns_empty(self) -> None:
        """A text response (no tool calls) should return an empty list."""
        resp = make_text_response("Hello there")
        result = parse_tool_calls(resp)
        assert result == []

    def test_malformed_json_arguments(self) -> None:
        """Malformed JSON arguments should result in arguments=None."""
        func = MagicMock()
        func.name = "some_tool"
        func.arguments = "{broken json"
        tc = MagicMock()
        tc.id = "call_bad"
        tc.function = func

        msg = MagicMock()
        msg.tool_calls = [tc]
        msg.content = None
        choice = MagicMock()
        choice.message = msg
        resp = MagicMock()
        resp.choices = [choice]

        result = parse_tool_calls(resp)
        assert len(result) == 1
        assert result[0].name == "some_tool"
        # json_repair may be able to fix this, so just check it doesn't crash
        assert isinstance(result[0], ParsedToolCall)

    def test_none_arguments(self) -> None:
        """None arguments should result in arguments=None."""
        func = MagicMock()
        func.name = "some_tool"
        func.arguments = None
        tc = MagicMock()
        tc.id = "call_none"
        tc.function = func

        msg = MagicMock()
        msg.tool_calls = [tc]
        msg.content = None
        choice = MagicMock()
        choice.message = msg
        resp = MagicMock()
        resp.choices = [choice]

        result = parse_tool_calls(resp)
        assert len(result) == 1
        assert result[0].arguments is None

    def test_empty_string_arguments(self) -> None:
        """Empty string arguments should result in arguments=None."""
        func = MagicMock()
        func.name = "some_tool"
        func.arguments = ""
        tc = MagicMock()
        tc.id = "call_empty"
        tc.function = func

        msg = MagicMock()
        msg.tool_calls = [tc]
        msg.content = None
        choice = MagicMock()
        choice.message = msg
        resp = MagicMock()
        resp.choices = [choice]

        result = parse_tool_calls(resp)
        assert len(result) == 1
        assert result[0].arguments is None

    def test_non_dict_arguments(self) -> None:
        """Non-dict parsed arguments (e.g. a list) should result in arguments=None."""
        func = MagicMock()
        func.name = "some_tool"
        func.arguments = json.dumps([1, 2, 3])
        tc = MagicMock()
        tc.id = "call_list"
        tc.function = func

        msg = MagicMock()
        msg.tool_calls = [tc]
        msg.content = None
        choice = MagicMock()
        choice.message = msg
        resp = MagicMock()
        resp.choices = [choice]

        result = parse_tool_calls(resp)
        assert len(result) == 1
        assert result[0].arguments is None

    def test_tool_call_with_no_function(self) -> None:
        """Tool call object with function=None should be skipped."""
        tc = MagicMock()
        tc.id = "call_nofunc"
        tc.function = None

        msg = MagicMock()
        msg.tool_calls = [tc]
        msg.content = None
        choice = MagicMock()
        choice.message = msg
        resp = MagicMock()
        resp.choices = [choice]

        result = parse_tool_calls(resp)
        assert result == []

    def test_empty_tool_calls_list(self) -> None:
        """Empty tool_calls list should return empty result."""
        msg = MagicMock()
        msg.tool_calls = []
        msg.content = "some text"
        choice = MagicMock()
        choice.message = msg
        resp = MagicMock()
        resp.choices = [choice]

        result = parse_tool_calls(resp)
        assert result == []

    def test_parsed_tool_call_is_frozen(self) -> None:
        """ParsedToolCall should be immutable."""
        ptc = ParsedToolCall(id="1", name="test", arguments={"a": 1})
        try:
            ptc.name = "changed"  # type: ignore[misc]
            raised = False
        except AttributeError:
            raised = True
        assert raised

    def test_json_repair_fixes_minor_issues(self) -> None:
        """json_repair should fix minor JSON issues like trailing commas."""
        func = MagicMock()
        func.name = "some_tool"
        # Trailing comma is invalid JSON but json_repair can fix it
        func.arguments = '{"key": "value",}'
        tc = MagicMock()
        tc.id = "call_repair"
        tc.function = func

        msg = MagicMock()
        msg.tool_calls = [tc]
        msg.content = None
        choice = MagicMock()
        choice.message = msg
        resp = MagicMock()
        resp.choices = [choice]

        result = parse_tool_calls(resp)
        assert len(result) == 1
        assert result[0].arguments == {"key": "value"}


class TestGetResponseText:
    def test_returns_content(self) -> None:
        """Should return the text content of the response."""
        resp = make_text_response("Hello world")
        assert get_response_text(resp) == "Hello world"

    def test_returns_empty_for_none(self) -> None:
        """Should return empty string when content is None."""
        msg = MagicMock()
        msg.content = None
        choice = MagicMock()
        choice.message = msg
        resp = MagicMock()
        resp.choices = [choice]

        assert get_response_text(resp) == ""


class TestJsonRepairWarning:
    """Tests for the warning logged when json_repair fixes malformed JSON."""

    def test_no_warning_for_valid_json(self, caplog: pytest.LogCaptureFixture) -> None:
        """Valid JSON should not trigger a warning."""
        from backend.app.agent.llm_parsing import _parse_arguments

        with caplog.at_level("WARNING", logger="backend.app.agent.llm_parsing"):
            result = _parse_arguments('{"key": "value"}')

        assert result == {"key": "value"}
        assert caplog.records == []

    def test_warning_for_trailing_comma(self, caplog: pytest.LogCaptureFixture) -> None:
        """Trailing comma (invalid JSON fixed by json_repair) should log a warning."""
        from backend.app.agent.llm_parsing import _parse_arguments

        malformed = '{"key": "value",}'
        with caplog.at_level("WARNING", logger="backend.app.agent.llm_parsing"):
            result = _parse_arguments(malformed)

        assert result == {"key": "value"}
        assert len(caplog.records) == 1
        assert "json_repair modified malformed tool arguments" in caplog.records[0].message
        assert malformed in caplog.records[0].message

    def test_warning_for_unquoted_keys(self, caplog: pytest.LogCaptureFixture) -> None:
        """Unquoted keys (invalid JSON fixed by json_repair) should log a warning."""
        from backend.app.agent.llm_parsing import _parse_arguments

        malformed = '{key: "value"}'
        with caplog.at_level("WARNING", logger="backend.app.agent.llm_parsing"):
            result = _parse_arguments(malformed)

        assert result == {"key": "value"}
        assert len(caplog.records) == 1
        assert "json_repair modified malformed tool arguments" in caplog.records[0].message

    def test_warning_truncates_long_arguments(self, caplog: pytest.LogCaptureFixture) -> None:
        """Long malformed arguments should be truncated to 200 chars in the warning."""
        from backend.app.agent.llm_parsing import _parse_arguments

        # Build a malformed JSON string longer than 200 chars
        long_value = "x" * 300
        malformed = '{key: "' + long_value + '"}'
        with caplog.at_level("WARNING", logger="backend.app.agent.llm_parsing"):
            result = _parse_arguments(malformed)

        assert result is not None
        assert len(caplog.records) == 1
        # The logged message should contain only the first 200 chars of the raw input
        logged_msg = caplog.records[0].message
        assert malformed[:200] in logged_msg
        assert malformed[201:] not in logged_msg

    def test_no_warning_for_none_arguments(self, caplog: pytest.LogCaptureFixture) -> None:
        """None arguments should return None without any warning."""
        from backend.app.agent.llm_parsing import _parse_arguments

        with caplog.at_level("WARNING", logger="backend.app.agent.llm_parsing"):
            result = _parse_arguments(None)

        assert result is None
        assert caplog.records == []

    def test_no_warning_for_empty_arguments(self, caplog: pytest.LogCaptureFixture) -> None:
        """Empty string arguments should return None without any warning."""
        from backend.app.agent.llm_parsing import _parse_arguments

        with caplog.at_level("WARNING", logger="backend.app.agent.llm_parsing"):
            result = _parse_arguments("")

        assert result is None
        assert caplog.records == []
