"""Tests for MessageBatcher: rapid-fire message batching per contractor."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.agent.file_store import ContractorData, SessionState, StoredMessage
from backend.app.agent.ingestion import MessageBatcher


class TestMessageBatcher:
    """Unit tests for the batching logic."""

    @pytest.mark.asyncio
    async def test_single_message_processed_after_window(self) -> None:
        """A single message should be processed after the batch window expires."""
        batcher = MessageBatcher(window_ms=50)
        messaging = MagicMock()

        mock_contractor = ContractorData(id=1, channel_identifier="123", phone="")

        mock_session = SessionState(session_id="sess-1", contractor_id=1)

        mock_message = StoredMessage(direction="inbound", body="hello")

        with (
            patch(
                "backend.app.agent.ingestion.handle_inbound_message",
                new_callable=AsyncMock,
            ) as mock_handle,
            patch("backend.app.agent.ingestion.contractor_locks") as mock_locks,
        ):
            mock_locks.acquire.return_value = AsyncMock(
                __aenter__=AsyncMock(), __aexit__=AsyncMock()
            )

            await batcher.enqueue(mock_contractor, mock_session, mock_message, [], messaging)
            await asyncio.sleep(0.1)

            mock_handle.assert_called_once()
            call_kwargs = mock_handle.call_args.kwargs
            assert call_kwargs["message"] is mock_message
            assert call_kwargs["media_urls"] == []

    @pytest.mark.asyncio
    async def test_multiple_messages_batched_into_one(self) -> None:
        """Rapid-fire messages should be batched: only the last triggers the pipeline."""
        batcher = MessageBatcher(window_ms=100)
        messaging = MagicMock()

        mock_contractor = ContractorData(id=1, channel_identifier="123", phone="")

        mock_session = SessionState(session_id="sess-1", contractor_id=1)

        mock_msg_1 = StoredMessage(direction="inbound", body="first")
        mock_msg_2 = StoredMessage(direction="inbound", body="second")
        mock_msg_3 = StoredMessage(direction="inbound", body="third")

        with (
            patch(
                "backend.app.agent.ingestion.handle_inbound_message",
                new_callable=AsyncMock,
            ) as mock_handle,
            patch("backend.app.agent.ingestion.contractor_locks") as mock_locks,
        ):
            mock_locks.acquire.return_value = AsyncMock(
                __aenter__=AsyncMock(), __aexit__=AsyncMock()
            )

            # Enqueue 3 messages rapidly (within the batch window)
            await batcher.enqueue(
                mock_contractor,
                mock_session,
                mock_msg_1,
                [("file_a", "image/jpeg")],
                messaging,
            )
            await batcher.enqueue(mock_contractor, mock_session, mock_msg_2, [], messaging)
            await batcher.enqueue(
                mock_contractor,
                mock_session,
                mock_msg_3,
                [("file_b", "audio/ogg")],
                messaging,
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
    async def test_different_contractors_not_batched(self) -> None:
        """Messages from different contractors should be processed independently."""
        batcher = MessageBatcher(window_ms=50)
        messaging = MagicMock()

        mock_c1 = ContractorData(id=1, channel_identifier="111", phone="")
        mock_c2 = ContractorData(id=2, channel_identifier="222", phone="")

        mock_session_1 = SessionState(session_id="sess-1", contractor_id=1)
        mock_session_2 = SessionState(session_id="sess-2", contractor_id=2)

        mock_msg_1 = StoredMessage(direction="inbound", body="from c1")
        mock_msg_2 = StoredMessage(direction="inbound", body="from c2")

        with (
            patch(
                "backend.app.agent.ingestion.handle_inbound_message",
                new_callable=AsyncMock,
            ) as mock_handle,
            patch("backend.app.agent.ingestion.contractor_locks") as mock_locks,
        ):
            mock_locks.acquire.return_value = AsyncMock(
                __aenter__=AsyncMock(), __aexit__=AsyncMock()
            )

            await batcher.enqueue(mock_c1, mock_session_1, mock_msg_1, [], messaging)
            await batcher.enqueue(mock_c2, mock_session_2, mock_msg_2, [], messaging)

            await asyncio.sleep(0.15)

            # Both contractors should get their own pipeline call
            assert mock_handle.call_count == 2

    @pytest.mark.asyncio
    async def test_timer_resets_on_new_message(self) -> None:
        """Adding a message should reset the batch window timer."""
        batcher = MessageBatcher(window_ms=100)
        messaging = MagicMock()

        mock_contractor = ContractorData(id=1, channel_identifier="123", phone="")

        mock_session = SessionState(session_id="sess-1", contractor_id=1)

        mock_msg_1 = StoredMessage(direction="inbound", body="first")
        mock_msg_2 = StoredMessage(direction="inbound", body="second")

        with (
            patch(
                "backend.app.agent.ingestion.handle_inbound_message",
                new_callable=AsyncMock,
            ) as mock_handle,
            patch("backend.app.agent.ingestion.contractor_locks") as mock_locks,
        ):
            mock_locks.acquire.return_value = AsyncMock(
                __aenter__=AsyncMock(), __aexit__=AsyncMock()
            )

            # First message
            await batcher.enqueue(mock_contractor, mock_session, mock_msg_1, [], messaging)

            # Wait 70ms (within the 100ms window)
            await asyncio.sleep(0.07)
            mock_handle.assert_not_called()

            # Second message resets the timer
            await batcher.enqueue(mock_contractor, mock_session, mock_msg_2, [], messaging)

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
        messaging = MagicMock()

        mock_contractor = ContractorData(id=1, channel_identifier="123", phone="")

        mock_session = SessionState(session_id="sess-1", contractor_id=1)

        mock_message = StoredMessage(direction="inbound", body="hello")

        with (
            patch(
                "backend.app.agent.ingestion.handle_inbound_message",
                new_callable=AsyncMock,
            ) as mock_handle,
            patch("backend.app.agent.ingestion.contractor_locks") as mock_locks,
        ):
            mock_locks.acquire.return_value = AsyncMock(
                __aenter__=AsyncMock(), __aexit__=AsyncMock()
            )

            await batcher.enqueue(mock_contractor, mock_session, mock_message, [], messaging)
            await asyncio.sleep(0.05)

            mock_handle.assert_called_once()
