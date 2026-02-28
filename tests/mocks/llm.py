from unittest.mock import MagicMock


def make_vision_response(
    description: str = "A 12x12 composite deck with cedar railing, showing minor weathering.",
) -> MagicMock:
    """Build a mock any-llm response for vision calls."""
    return _make_completion_response(description)


def make_text_response(content: str = "I'll help you with that.") -> MagicMock:
    """Build a mock any-llm response for text calls."""
    return _make_completion_response(content)


def _make_completion_response(content: str) -> MagicMock:
    """Build a mock ChatCompletion response."""
    msg = MagicMock()
    msg.content = content
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    return resp
