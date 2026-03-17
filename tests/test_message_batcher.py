"""Tests for MessageBatcher: rapid-fire message batching per user."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.agent.file_store import SessionState, StoredMessage
from backend.app.agent.ingestion import InboundMessage, MessageBatcher, process_inbound_from_bus
from backend.app.bus import message_bus
from backend.app.models import User


class TestMessageBatcher:
    """Unit tests for the batching logic."""

    @pytest.mark.asyncio
    async def test_single_message_processed_after_window(self) -> None:
        """A single message should be processed after the batch window expires."""
        batcher = MessageBatcher(window_ms=50)
        mock_user = User(id="1", channel_identifier="123", phone="")

        mock_session = SessionState(session_id="sess-1", user_id="1")

        mock_message = StoredMessage(direction="inbound", body="hello")

        with (
            patch(
                "backend.app.agent.ingestion.handle_inbound_message",
                new_callable=AsyncMock,
            ) as mock_handle,
            patch("backend.app.agent.ingestion.user_locks") as mock_locks,
        ):
            mock_locks.acquire.return_value = AsyncMock(
                __aenter__=AsyncMock(), __aexit__=AsyncMock()
            )

            await batcher.enqueue(mock_user, mock_session, mock_message, [], "telegram")
            await asyncio.sleep(0.1)

            mock_handle.assert_called_once()
            call_kwargs = mock_handle.call_args.kwargs
            assert call_kwargs["message"] is mock_message
            assert call_kwargs["media_urls"] == []

    @pytest.mark.asyncio
    async def test_multiple_messages_batched_into_one(self) -> None:
        """Rapid-fire messages should be batched: only the last triggers the pipeline."""
        batcher = MessageBatcher(window_ms=100)
        mock_user = User(id="1", channel_identifier="123", phone="")

        mock_session = SessionState(session_id="sess-1", user_id="1")

        mock_msg_1 = StoredMessage(direction="inbound", body="first")
        mock_msg_2 = StoredMessage(direction="inbound", body="second")
        mock_msg_3 = StoredMessage(direction="inbound", body="third")

        with (
            patch(
                "backend.app.agent.ingestion.handle_inbound_message",
                new_callable=AsyncMock,
            ) as mock_handle,
            patch("backend.app.agent.ingestion.user_locks") as mock_locks,
        ):
            mock_locks.acquire.return_value = AsyncMock(
                __aenter__=AsyncMock(), __aexit__=AsyncMock()
            )

            # Enqueue 3 messages rapidly (within the batch window)
            await batcher.enqueue(
                mock_user,
                mock_session,
                mock_msg_1,
                [("file_a", "image/jpeg")],
                "telegram",
            )
            await batcher.enqueue(mock_user, mock_session, mock_msg_2, [], "telegram")
            await batcher.enqueue(
                mock_user,
                mock_session,
                mock_msg_3,
                [("file_b", "audio/ogg")],
                "telegram",
            )

            await asyncio.sleep(0.2)

            # Only one pipeline call should happen (for the last message)
            mock_handle.assert_called_once()
            call_kwargs = mock_handle.call_args.kwargs
            assert call_kwargs["message"] is mock_msg_3

            # Media from all messages should be merged
            assert call_kwargs["media_urls"] == [
                ("file_a", "image/jpeg"),
                ("file_b", "audio/ogg"),
            ]

    @pytest.mark.asyncio
    async def test_different_users_not_batched(self) -> None:
        """Messages from different users should be processed independently."""
        batcher = MessageBatcher(window_ms=50)

        mock_c1 = User(id="1", channel_identifier="111", phone="")
        mock_c2 = User(id="2", channel_identifier="222", phone="")

        mock_session_1 = SessionState(session_id="sess-1", user_id="1")
        mock_session_2 = SessionState(session_id="sess-2", user_id="2")

        mock_msg_1 = StoredMessage(direction="inbound", body="from c1")
        mock_msg_2 = StoredMessage(direction="inbound", body="from c2")

        with (
            patch(
                "backend.app.agent.ingestion.handle_inbound_message",
                new_callable=AsyncMock,
            ) as mock_handle,
            patch("backend.app.agent.ingestion.user_locks") as mock_locks,
        ):
            mock_locks.acquire.return_value = AsyncMock(
                __aenter__=AsyncMock(), __aexit__=AsyncMock()
            )

            await batcher.enqueue(mock_c1, mock_session_1, mock_msg_1, [], "telegram")
            await batcher.enqueue(mock_c2, mock_session_2, mock_msg_2, [], "telegram")

            await asyncio.sleep(0.15)

            # Both users should get their own pipeline call
            assert mock_handle.call_count == 2

    @pytest.mark.asyncio
    async def test_timer_resets_on_new_message(self) -> None:
        """Adding a message should reset the batch window timer."""
        batcher = MessageBatcher(window_ms=100)
        mock_user = User(id="1", channel_identifier="123", phone="")

        mock_session = SessionState(session_id="sess-1", user_id="1")

        mock_msg_1 = StoredMessage(direction="inbound", body="first")
        mock_msg_2 = StoredMessage(direction="inbound", body="second")

        with (
            patch(
                "backend.app.agent.ingestion.handle_inbound_message",
                new_callable=AsyncMock,
            ) as mock_handle,
            patch("backend.app.agent.ingestion.user_locks") as mock_locks,
        ):
            mock_locks.acquire.return_value = AsyncMock(
                __aenter__=AsyncMock(), __aexit__=AsyncMock()
            )

            # First message
            await batcher.enqueue(mock_user, mock_session, mock_msg_1, [], "telegram")

            # Wait 70ms (within the 100ms window)
            await asyncio.sleep(0.07)
            mock_handle.assert_not_called()

            # Second message resets the timer
            await batcher.enqueue(mock_user, mock_session, mock_msg_2, [], "telegram")

            # Wait 70ms again (still within the new 100ms window)
            await asyncio.sleep(0.07)
            mock_handle.assert_not_called()

            # Wait for the window to expire
            await asyncio.sleep(0.1)
            mock_handle.assert_called_once()

    @pytest.mark.asyncio
    async def test_zero_window_processes_immediately(self) -> None:
        """A zero window should process messages without batching delay."""
        batcher = MessageBatcher(window_ms=0)
        mock_user = User(id="1", channel_identifier="123", phone="")

        mock_session = SessionState(session_id="sess-1", user_id="1")

        mock_message = StoredMessage(direction="inbound", body="hello")

        with (
            patch(
                "backend.app.agent.ingestion.handle_inbound_message",
                new_callable=AsyncMock,
            ) as mock_handle,
            patch("backend.app.agent.ingestion.user_locks") as mock_locks,
        ):
            mock_locks.acquire.return_value = AsyncMock(
                __aenter__=AsyncMock(), __aexit__=AsyncMock()
            )

            await batcher.enqueue(mock_user, mock_session, mock_message, [], "telegram")
            await asyncio.sleep(0.05)

            mock_handle.assert_called_once()

    @pytest.mark.asyncio
    async def test_pipeline_failure_sends_fallback_error(self) -> None:
        """When the agent pipeline raises, a fallback error is sent via the bus."""
        batcher = MessageBatcher(window_ms=50)

        mock_user = User(id="1", channel_identifier="123", phone="")
        mock_session = SessionState(session_id="sess-1", user_id="1")
        mock_message = StoredMessage(direction="inbound", body="hello")

        with (
            patch(
                "backend.app.agent.ingestion.handle_inbound_message",
                new_callable=AsyncMock,
                side_effect=RuntimeError("LLM API down"),
            ),
            patch("backend.app.agent.ingestion.user_locks") as mock_locks,
        ):
            mock_locks.acquire.return_value = AsyncMock(
                __aenter__=AsyncMock(), __aexit__=AsyncMock()
            )

            await batcher.enqueue(mock_user, mock_session, mock_message, [], "telegram")
            await asyncio.sleep(0.15)

            # Fallback error published to bus
            found = False
            while not message_bus.outbound.empty():
                outbound = message_bus.outbound.get_nowait()
                if not outbound.is_typing_indicator:
                    assert outbound.chat_id == "123"
                    assert "something went wrong" in outbound.content.lower()
                    found = True
                    break
            assert found

    @pytest.mark.asyncio
    async def test_pipeline_failure_fallback_send_also_fails(self) -> None:
        """When both the pipeline and bus publish fail, no exception propagates."""
        batcher = MessageBatcher(window_ms=50)

        mock_user = User(id="1", channel_identifier="123", phone="")
        mock_session = SessionState(session_id="sess-1", user_id="1")
        mock_message = StoredMessage(direction="inbound", body="hello")

        mock_bus = MagicMock()
        mock_bus.publish_outbound = AsyncMock(side_effect=RuntimeError("bus down"))

        with (
            patch(
                "backend.app.agent.ingestion.handle_inbound_message",
                new_callable=AsyncMock,
                side_effect=RuntimeError("LLM API down"),
            ),
            patch("backend.app.agent.ingestion.user_locks") as mock_locks,
            patch("backend.app.bus.message_bus", mock_bus),
        ):
            mock_locks.acquire.return_value = AsyncMock(
                __aenter__=AsyncMock(), __aexit__=AsyncMock()
            )

            # Should not raise even when both pipeline and fallback fail
            await batcher.enqueue(mock_user, mock_session, mock_message, [], "telegram")
            await asyncio.sleep(0.15)


class TestProcessInboundFallbackError:
    """Tests for error fallback in process_inbound_from_bus (non-batcher path)."""

    @pytest.mark.asyncio
    async def test_pipeline_failure_sends_fallback_error(self) -> None:
        """When the agent pipeline raises in the non-batcher path, a fallback is sent."""
        inbound = InboundMessage(
            channel="telegram",
            sender_id="456",
            text="hi there",
        )

        mock_user = User(id="1", channel_identifier="456", phone="")
        mock_session = SessionState(session_id="sess-1", user_id="1")
        mock_message = StoredMessage(direction="inbound", body="hi there")

        with (
            patch(
                "backend.app.agent.ingestion._get_or_create_user",
                new_callable=AsyncMock,
                return_value=mock_user,
            ),
            patch(
                "backend.app.agent.ingestion.get_approval_gate",
            ) as mock_gate,
            patch(
                "backend.app.agent.ingestion.get_or_create_conversation",
                new_callable=AsyncMock,
                return_value=(mock_session, True),
            ),
            patch(
                "backend.app.agent.ingestion.get_session_store",
            ) as mock_store_fn,
            patch(
                "backend.app.agent.ingestion.settings",
            ) as mock_settings,
            patch(
                "backend.app.agent.ingestion.handle_inbound_message",
                new_callable=AsyncMock,
                side_effect=RuntimeError("LLM API down"),
            ),
            patch("backend.app.agent.ingestion.user_locks") as mock_locks,
        ):
            mock_gate.return_value.has_pending.return_value = False
            mock_session_store = AsyncMock()
            mock_session_store.add_message.return_value = mock_message
            mock_store_fn.return_value = mock_session_store
            mock_settings.message_batch_window_ms = 0
            mock_locks.acquire.return_value = AsyncMock(
                __aenter__=AsyncMock(), __aexit__=AsyncMock()
            )

            await process_inbound_from_bus(inbound)

            # Fallback error published to bus
            found = False
            while not message_bus.outbound.empty():
                outbound = message_bus.outbound.get_nowait()
                if not outbound.is_typing_indicator:
                    assert outbound.chat_id == "456"
                    assert "something went wrong" in outbound.content.lower()
                    found = True
                    break
            assert found
