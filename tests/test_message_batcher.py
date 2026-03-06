"""Tests for MessageBatcher: rapid-fire message batching per contractor."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.agent.ingestion import MessageBatcher


class TestMessageBatcher:
    """Unit tests for the batching logic."""

    @pytest.mark.asyncio
    async def test_single_message_processed_after_window(self) -> None:
        """A single message should be processed after the batch window expires."""
        batcher = MessageBatcher(window_ms=50)
        messaging = MagicMock()

        mock_contractor = MagicMock()
        mock_contractor.id = 1
        mock_contractor.channel_identifier = "123"
        mock_contractor.phone = ""

        mock_message = MagicMock()
        mock_message.id = 10
        mock_message.conversation_id = 1

        mock_db = MagicMock()
        mock_db.get = MagicMock(
            side_effect=lambda model, pk: {
                (type(mock_contractor), 1): mock_contractor,
                (type(mock_message), 10): mock_message,
            }.get((model, pk))
        )

        with (
            patch("backend.app.agent.ingestion.SessionLocal", return_value=mock_db),
            patch(
                "backend.app.agent.ingestion.handle_inbound_message",
                new_callable=AsyncMock,
            ) as mock_handle,
            patch("backend.app.agent.ingestion.contractor_locks") as mock_locks,
        ):
            mock_locks.acquire.return_value = AsyncMock(
                __aenter__=AsyncMock(), __aexit__=AsyncMock()
            )
            # Use the actual model classes for db.get
            from backend.app.models import Contractor, Message

            mock_db.get = MagicMock(
                side_effect=lambda model, pk: {
                    (Contractor, 1): mock_contractor,
                    (Message, 10): mock_message,
                }.get((model, pk))
            )

            await batcher.enqueue(1, 10, [], messaging)
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

        mock_contractor = MagicMock()
        mock_contractor.id = 1
        mock_contractor.channel_identifier = "123"
        mock_contractor.phone = ""

        mock_msg_1 = MagicMock()
        mock_msg_1.id = 10
        mock_msg_2 = MagicMock()
        mock_msg_2.id = 11
        mock_msg_3 = MagicMock()
        mock_msg_3.id = 12
        mock_msg_3.conversation_id = 1

        from backend.app.models import Contractor, Message

        mock_db = MagicMock()
        mock_db.get = MagicMock(
            side_effect=lambda model, pk: {
                (Contractor, 1): mock_contractor,
                (Message, 12): mock_msg_3,
            }.get((model, pk))
        )

        with (
            patch("backend.app.agent.ingestion.SessionLocal", return_value=mock_db),
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
            await batcher.enqueue(1, 10, [("file_a", "image/jpeg")], messaging)
            await batcher.enqueue(1, 11, [], messaging)
            await batcher.enqueue(1, 12, [("file_b", "audio/ogg")], messaging)

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

        mock_c1 = MagicMock()
        mock_c1.id = 1
        mock_c1.channel_identifier = "111"
        mock_c1.phone = ""

        mock_c2 = MagicMock()
        mock_c2.id = 2
        mock_c2.channel_identifier = "222"
        mock_c2.phone = ""

        mock_msg_1 = MagicMock()
        mock_msg_1.id = 10
        mock_msg_1.conversation_id = 1

        mock_msg_2 = MagicMock()
        mock_msg_2.id = 20
        mock_msg_2.conversation_id = 2

        from backend.app.models import Contractor, Message

        mock_db = MagicMock()
        mock_db.get = MagicMock(
            side_effect=lambda model, pk: {
                (Contractor, 1): mock_c1,
                (Contractor, 2): mock_c2,
                (Message, 10): mock_msg_1,
                (Message, 20): mock_msg_2,
            }.get((model, pk))
        )

        with (
            patch("backend.app.agent.ingestion.SessionLocal", return_value=mock_db),
            patch(
                "backend.app.agent.ingestion.handle_inbound_message",
                new_callable=AsyncMock,
            ) as mock_handle,
            patch("backend.app.agent.ingestion.contractor_locks") as mock_locks,
        ):
            mock_locks.acquire.return_value = AsyncMock(
                __aenter__=AsyncMock(), __aexit__=AsyncMock()
            )

            await batcher.enqueue(1, 10, [], messaging)
            await batcher.enqueue(2, 20, [], messaging)

            await asyncio.sleep(0.15)

            # Both contractors should get their own pipeline call
            assert mock_handle.call_count == 2

    @pytest.mark.asyncio
    async def test_timer_resets_on_new_message(self) -> None:
        """Adding a message should reset the batch window timer."""
        batcher = MessageBatcher(window_ms=100)
        messaging = MagicMock()

        mock_contractor = MagicMock()
        mock_contractor.id = 1
        mock_contractor.channel_identifier = "123"
        mock_contractor.phone = ""

        mock_msg = MagicMock()
        mock_msg.id = 11
        mock_msg.conversation_id = 1

        from backend.app.models import Contractor, Message

        mock_db = MagicMock()
        mock_db.get = MagicMock(
            side_effect=lambda model, pk: {
                (Contractor, 1): mock_contractor,
                (Message, 11): mock_msg,
            }.get((model, pk))
        )

        with (
            patch("backend.app.agent.ingestion.SessionLocal", return_value=mock_db),
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
            await batcher.enqueue(1, 10, [], messaging)

            # Wait 70ms (within the 100ms window)
            await asyncio.sleep(0.07)
            mock_handle.assert_not_called()

            # Second message resets the timer
            await batcher.enqueue(1, 11, [], messaging)

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

        mock_contractor = MagicMock()
        mock_contractor.id = 1
        mock_contractor.channel_identifier = "123"
        mock_contractor.phone = ""

        mock_msg = MagicMock()
        mock_msg.id = 10
        mock_msg.conversation_id = 1

        from backend.app.models import Contractor, Message

        mock_db = MagicMock()
        mock_db.get = MagicMock(
            side_effect=lambda model, pk: {
                (Contractor, 1): mock_contractor,
                (Message, 10): mock_msg,
            }.get((model, pk))
        )

        with (
            patch("backend.app.agent.ingestion.SessionLocal", return_value=mock_db),
            patch(
                "backend.app.agent.ingestion.handle_inbound_message",
                new_callable=AsyncMock,
            ) as mock_handle,
            patch("backend.app.agent.ingestion.contractor_locks") as mock_locks,
        ):
            mock_locks.acquire.return_value = AsyncMock(
                __aenter__=AsyncMock(), __aexit__=AsyncMock()
            )

            await batcher.enqueue(1, 10, [], messaging)
            await asyncio.sleep(0.05)

            mock_handle.assert_called_once()
