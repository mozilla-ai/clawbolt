"""Tests for channel base class and protocol conformance."""

import pytest

from backend.app.channels.base import BaseChannel
from backend.app.channels.telegram import TelegramChannel
from backend.app.services.messaging import MessagingService


def test_base_channel_cannot_be_instantiated() -> None:
    """BaseChannel is abstract and should not be directly instantiable."""
    with pytest.raises(TypeError):
        BaseChannel()


def test_telegram_channel_satisfies_messaging_protocol() -> None:
    """TelegramChannel should satisfy the MessagingService protocol."""
    channel = TelegramChannel(bot_token="test-token")
    assert isinstance(channel, MessagingService)
