"""Tests for the progressive approval system."""

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import BaseModel

from backend.app.agent.approval import (
    AMBIGUOUS_APPROVAL_REPLY,
    ApprovalDecision,
    ApprovalGate,
    ApprovalPolicy,
    ApprovalStore,
    PermissionLevel,
    _parse_approval_response,
    classify_approval_response,
    format_approval_message,
    get_approval_gate,
    get_approval_store,
    reset_approval_gate,
)
from backend.app.agent.concurrency import user_locks
from backend.app.agent.core import ClawboltAgent
from backend.app.agent.ingestion import (
    InboundMessage,
    _dispatch_to_pipeline,
    process_inbound_from_bus,
)
from backend.app.agent.tools.base import Tool, ToolResult
from backend.app.bus import OutboundMessage, message_bus
from backend.app.database import db_session_async
from backend.app.models import User
from tests.mocks.llm import make_text_response, make_tool_call_response

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _await_pending(gate: ApprovalGate, user_id: str, timeout: float = 2.0) -> None:
    """Block until ``gate.has_pending(user_id)`` is True.

    ``request_approval`` performs two DB writes (audit + orphan-detection
    row) before flipping ``has_pending`` to True, so a fixed sleep in a
    test can race ahead of registration when those commits take longer
    than expected. Polling on the same predicate the production poll
    task uses (``_interrupt_stale_approval`` in ``ingestion.py``) avoids
    that flake without weakening the assertion the test is making.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while not gate.has_pending(user_id):
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"has_pending({user_id!r}) never became True within {timeout}s")
        await asyncio.sleep(0.001)


class _EchoParams(BaseModel):
    text: str


async def _echo_tool(text: str) -> ToolResult:
    return ToolResult(content=f"echo: {text}")


class _UrlParams(BaseModel):
    url: str


async def _fetch_tool(url: str) -> ToolResult:
    return ToolResult(content=f"fetched: {url}")


def _extract_domain(args: dict[str, object]) -> str | None:
    from urllib.parse import urlparse

    url = str(args.get("url", ""))
    parsed = urlparse(url)
    return parsed.netloc or None


def _describe_fetch(args: dict[str, object]) -> str:
    return f"fetch content from {args.get('url', 'unknown URL')}"


# ---------------------------------------------------------------------------
# ApprovalStore
# ---------------------------------------------------------------------------


class TestApprovalStore:
    async def test_default_permission(self, tmp_path: object) -> None:
        store = ApprovalStore()
        level = await store.check_permission("1", "web_search", default=PermissionLevel.ASK)
        assert level == PermissionLevel.ASK

    async def test_tool_level_override(self, tmp_path: object) -> None:
        store = ApprovalStore()
        await store.set_permission("1", "web_search", PermissionLevel.ALWAYS)
        level = await store.check_permission("1", "web_search", default=PermissionLevel.ASK)
        assert level == PermissionLevel.ALWAYS

    async def test_resource_level_override(self, tmp_path: object) -> None:
        store = ApprovalStore()
        await store.set_permission(
            "1", "web_fetch", PermissionLevel.ALWAYS, resource="homedepot.com"
        )
        level = await store.check_permission(
            "1", "web_fetch", resource="homedepot.com", default=PermissionLevel.ASK
        )
        assert level == PermissionLevel.ALWAYS

    async def test_glob_matching(self, tmp_path: object) -> None:
        store = ApprovalStore()
        await store.set_permission("1", "web_fetch", PermissionLevel.ALWAYS, resource="*.gov")
        level = await store.check_permission(
            "1", "web_fetch", resource="permits.gov", default=PermissionLevel.ASK
        )
        assert level == PermissionLevel.ALWAYS

    async def test_resource_priority_over_tool(self, tmp_path: object) -> None:
        store = ApprovalStore()
        await store.set_permission("1", "web_fetch", PermissionLevel.NEVER)
        await store.set_permission("1", "web_fetch", PermissionLevel.ALWAYS, resource="safe.com")
        level = await store.check_permission(
            "1", "web_fetch", resource="safe.com", default=PermissionLevel.ASK
        )
        assert level == PermissionLevel.ALWAYS

    async def test_falls_through_to_tool_when_no_resource_match(self, tmp_path: object) -> None:
        store = ApprovalStore()
        await store.set_permission("1", "web_fetch", PermissionLevel.NEVER)
        level = await store.check_permission(
            "1", "web_fetch", resource="unknown.com", default=PermissionLevel.ASK
        )
        assert level == PermissionLevel.NEVER

    async def test_persistence_round_trip(self, tmp_path: object) -> None:
        store1 = ApprovalStore()
        await store1.set_permission("1", "web_search", PermissionLevel.ALWAYS)
        await store1.set_permission("1", "web_fetch", PermissionLevel.NEVER, resource="evil.com")

        store2 = ApprovalStore()
        assert await store2.check_permission("1", "web_search") == PermissionLevel.ALWAYS
        assert (
            await store2.check_permission("1", "web_fetch", resource="evil.com")
            == PermissionLevel.NEVER
        )


# ---------------------------------------------------------------------------
# ApprovalStore: generate_defaults / ensure_complete / reset_permissions
# ---------------------------------------------------------------------------


class TestApprovalStoreComplete:
    def test_generate_defaults_includes_all_tools(self, tmp_path: object) -> None:
        """generate_defaults returns a dict with all registered tools."""
        from backend.app.agent.tools.registry import (
            default_registry,
            ensure_tool_modules_imported,
        )

        ensure_tool_modules_imported()
        store = ApprovalStore()
        defaults = store.generate_defaults("gen-user")
        assert defaults["version"] == 1
        assert isinstance(defaults["tools"], dict)
        assert len(defaults["tools"]) > 0
        # Every registered sub-tool should be present
        for factory_name in default_registry.factory_names:
            for st in default_registry.get_factory_sub_tools(factory_name):
                assert st.name in defaults["tools"]

    async def test_ensure_complete_backfills_missing(self, tmp_path: object) -> None:
        """ensure_complete adds new tools to an existing file."""
        store = ApprovalStore()
        # Start with a partial file
        await store._save(
            "backfill-user", {"version": 1, "tools": {"send_media_reply": "never"}, "resources": {}}
        )
        data = await store.ensure_complete("backfill-user")
        # send_media_reply should keep its override
        assert data["tools"]["send_media_reply"] == "never"
        # Other tools should have been backfilled
        assert len(data["tools"]) > 1

    async def test_ensure_complete_preserves_overrides(self, tmp_path: object) -> None:
        """ensure_complete does not overwrite user customizations."""
        store = ApprovalStore()
        await store._save(
            "preserve-user",
            {
                "version": 1,
                "tools": {"send_media_reply": "never", "read_file": "ask"},
                "resources": {"web_fetch": {"evil.com": "never"}},
            },
        )
        data = await store.ensure_complete("preserve-user")
        assert data["tools"]["send_media_reply"] == "never"
        assert data["tools"]["read_file"] == "ask"
        assert data["resources"]["web_fetch"]["evil.com"] == "never"

    async def test_reset_permissions_writes_defaults(self, tmp_path: object) -> None:
        """reset_permissions replaces everything with defaults."""
        store = ApprovalStore()
        await store.set_permission("reset-user", "send_media_reply", PermissionLevel.NEVER)
        await store.reset_permissions("reset-user")
        data = await store._load("reset-user")
        # send_media_reply should be back to its default, not deny
        defaults = store.generate_defaults("reset-user")
        assert data["tools"]["send_media_reply"] == defaults["tools"]["send_media_reply"]

    async def test_set_permission_preserves_complete_file(self, tmp_path: object) -> None:
        """set_permission does not lose other entries."""
        store = ApprovalStore()
        await store.ensure_complete("set-perm-user")
        defaults = store.generate_defaults("set-perm-user")
        original_count = len(defaults["tools"])

        await store.set_permission("set-perm-user", "send_media_reply", PermissionLevel.NEVER)
        data = await store._load("set-perm-user")
        # All tools should still be present
        assert len(data["tools"]) >= original_count
        assert data["tools"]["send_media_reply"] == "never"


# ---------------------------------------------------------------------------
# ApprovalStore: pg_advisory_xact_lock concurrency regression
# ---------------------------------------------------------------------------


# Note: the threaded ``TestApprovalLockSerialization`` class was removed
# alongside the sync ``_lock_user_permissions`` helper (issue #1234).
# The async port lives in ``tests/test_approval_async.py::
# TestApprovalLockSerializationAsync`` and exercises the same matrix
# (same-user serializes, different-user runs in parallel) against the
# now-async ``_lock_user_permissions`` helper.
class _TestApprovalLockSerializationRemoved:
    """Placeholder so this section's docstring stays in repo history."""


# ---------------------------------------------------------------------------
# _parse_approval_response
# ---------------------------------------------------------------------------


class TestParseApprovalResponse:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("yes", ApprovalDecision.APPROVED),
            ("y", ApprovalDecision.APPROVED),
            ("Yes", ApprovalDecision.APPROVED),
            ("YES", ApprovalDecision.APPROVED),
            ("  y  ", ApprovalDecision.APPROVED),
            ("always", ApprovalDecision.ALWAYS_ALLOW),
            ("Always", ApprovalDecision.ALWAYS_ALLOW),
            ("no", ApprovalDecision.DENIED),
            ("n", ApprovalDecision.DENIED),
            ("No", ApprovalDecision.DENIED),
            ("never", ApprovalDecision.ALWAYS_DENY),
            ("Never", ApprovalDecision.ALWAYS_DENY),
        ],
    )
    def test_valid_responses(self, text: str, expected: ApprovalDecision) -> None:
        assert _parse_approval_response(text) == expected

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            # Compound replies users naturally type when they read
            # "yes or no (always/never to remember)" as a 2-axis answer.
            # All map to the unambiguous remember-this-decision result.
            ("yes always", ApprovalDecision.ALWAYS_ALLOW),
            ("Yes always", ApprovalDecision.ALWAYS_ALLOW),
            ("always yes", ApprovalDecision.ALWAYS_ALLOW),
            ("always allow", ApprovalDecision.ALWAYS_ALLOW),
            ("allow always", ApprovalDecision.ALWAYS_ALLOW),
            ("yes  always", ApprovalDecision.ALWAYS_ALLOW),  # collapse whitespace
            # The y/n shortcuts also pair with always/never.
            ("y always", ApprovalDecision.ALWAYS_ALLOW),
            ("always y", ApprovalDecision.ALWAYS_ALLOW),
            ("no never", ApprovalDecision.ALWAYS_DENY),
            ("Never no", ApprovalDecision.ALWAYS_DENY),
            ("n never", ApprovalDecision.ALWAYS_DENY),
            ("never n", ApprovalDecision.ALWAYS_DENY),
            ("never allow", ApprovalDecision.ALWAYS_DENY),
            ("deny always", ApprovalDecision.ALWAYS_DENY),
        ],
    )
    def test_compound_responses(self, text: str, expected: ApprovalDecision) -> None:
        """Two-word natural replies that match the prompt's option list
        should resolve at the fast path, not the LLM classifier."""
        assert _parse_approval_response(text) == expected

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            # Blanket "always all" replies grant a tool-level approval that
            # covers every resource (e.g. all invoice recipients), not just
            # the one in front of the user. These resolve at the fast path.
            ("always all", ApprovalDecision.ALWAYS_ALLOW_ALL),
            ("Always all", ApprovalDecision.ALWAYS_ALLOW_ALL),
            ("all always", ApprovalDecision.ALWAYS_ALLOW_ALL),
            ("allow all", ApprovalDecision.ALWAYS_ALLOW_ALL),
            ("always everyone", ApprovalDecision.ALWAYS_ALLOW_ALL),
            ("always anyone", ApprovalDecision.ALWAYS_ALLOW_ALL),
            ("always all.", ApprovalDecision.ALWAYS_ALLOW_ALL),  # punctuation stripped
            ("always  all", ApprovalDecision.ALWAYS_ALLOW_ALL),  # whitespace collapsed
        ],
    )
    def test_blanket_allow_all_responses(self, text: str, expected: ApprovalDecision) -> None:
        """ "always all" and its natural variants resolve to a blanket
        tool-level approval at the fast path, not the LLM classifier."""
        assert _parse_approval_response(text) == expected

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            # Trailing punctuation users type out of habit. All these
            # should route through the fast path, not the LLM classifier.
            ("Yes.", ApprovalDecision.APPROVED),
            ("yes!", ApprovalDecision.APPROVED),
            ("yes?", ApprovalDecision.APPROVED),
            ("No.", ApprovalDecision.DENIED),
            ("Always.", ApprovalDecision.ALWAYS_ALLOW),
            ("Never!", ApprovalDecision.ALWAYS_DENY),
            # The literal user phrasing from the original report.
            ("yes, always", ApprovalDecision.ALWAYS_ALLOW),
            ("Yes, always.", ApprovalDecision.ALWAYS_ALLOW),
            ("no, never", ApprovalDecision.ALWAYS_DENY),
            # Multiple punctuation chars also strip cleanly.
            ("yes!?", ApprovalDecision.APPROVED),
            ("Yes; always.", ApprovalDecision.ALWAYS_ALLOW),
        ],
    )
    def test_responses_with_punctuation(self, text: str, expected: ApprovalDecision) -> None:
        """Punctuation must be stripped before fast-path lookup; the user's
        natural phrasing ("yes, always" — the exact wording from the
        original report) must not fall through to the LLM classifier."""
        assert _parse_approval_response(text) == expected

    @pytest.mark.parametrize(
        "text",
        [
            # Mixed-axis pairs (one allow, one deny) are intentionally
            # NOT in the fast-path mapping; they go to the LLM classifier
            # so we don't silently pick the wrong direction.
            "yes never",
            "no always",
        ],
    )
    def test_mixed_axis_pairs_fall_through(self, text: str) -> None:
        """Conflicting compound pairs return None so they hit the LLM
        classifier rather than getting silently misclassified."""
        assert _parse_approval_response(text) is None

    @pytest.mark.parametrize("text", ["maybe", "sure", "ok", "hello", ""])
    def test_unrecognized_returns_none(self, text: str) -> None:
        assert _parse_approval_response(text) is None


# ---------------------------------------------------------------------------
# format_approval_message
# ---------------------------------------------------------------------------


class TestFormatApprovalMessage:
    def test_output_format(self) -> None:
        msg = format_approval_message("web_fetch", "fetch content from https://example.com")
        assert "fetch content from https://example.com" in msg
        assert "yes" in msg
        assert "no" in msg
        assert "always" in msg
        assert "never" in msg

    def test_options_are_a_menu_not_a_two_axis_answer(self) -> None:
        """Regression on the 'yes always' / 'no never' confusion.

        The previous wording ``"Reply yes or no (always/never to
        remember your choice)"`` got read as a two-axis question (pick
        an allow/deny axis AND a once/remember axis), so users typed
        ``yes always``. The new wording lists four distinct options on
        their own lines so the menu shape is obvious.
        """
        msg = format_approval_message("any_tool", "do the thing")
        # Each option appears on its own line.
        for option in ("  yes", "  no", "  always", "  never"):
            assert option in msg, f"missing menu line: {option!r}"
        # Old confusing wording is gone.
        assert "(always/never to remember" not in msg
        # Tool name should NOT appear in the user-facing message
        assert "web_fetch" not in msg

    def test_menu_uses_no_em_dashes(self) -> None:
        """The four-line menu must not contain em dashes.

        Per the project style rule on user-facing copy, separators in
        prose are colons, periods, or commas, not em dashes (which
        users on some clients render as a literal box). This test pins
        the punctuation so a future edit cannot quietly reintroduce
        them.
        """
        msg = format_approval_message("any_tool", "do the thing")
        assert "—" not in msg  # em dash
        assert " -- " not in msg  # double-hyphen approximation

    def test_blanket_option_absent_by_default(self) -> None:
        """The "always all" line only appears when explicitly offered."""
        msg = format_approval_message("any_tool", "do the thing")
        assert "always all" not in msg

    def test_blanket_option_shown_with_noun(self) -> None:
        """offer_blanket adds an "always all" line using the resource noun."""
        msg = format_approval_message(
            "qb_send",
            "Send Invoice to alice@example.com via QuickBooks",
            offer_blanket=True,
            resource_noun="recipients",
        )
        assert "  always all: allow for all recipients and remember" in msg
        # The per-target "always" line is still there and distinct.
        assert "  always: allow and remember" in msg

    def test_blanket_option_falls_back_when_noun_missing(self) -> None:
        """Without a resource noun the line uses a generic phrasing."""
        msg = format_approval_message("some_tool", "do it", offer_blanket=True)
        assert "  always all: allow for all of them and remember" in msg

    def test_blanket_prompt_keeps_never_as_trailer(self) -> None:
        """The blanket option must be inserted before the "never" line so
        context.py can still identify stored approval prompts in history by
        their trailing menu line (issue #1049 filter)."""
        from backend.app.agent.context import _is_approval_prompt

        msg = format_approval_message(
            "qb_send",
            "Send Invoice to alice@example.com via QuickBooks",
            offer_blanket=True,
            resource_noun="recipients",
        )
        assert msg.rstrip().endswith("never: deny and remember")
        assert _is_approval_prompt(msg)


# ---------------------------------------------------------------------------
# ApprovalGate
# ---------------------------------------------------------------------------


class TestApprovalGate:
    @pytest.mark.asyncio()
    async def test_resolve_sets_event_and_decision(self) -> None:
        gate = ApprovalGate()
        mock_publish = AsyncMock()

        async def _resolve_soon() -> None:
            await _await_pending(gate, "1")
            await gate.resolve("1", ApprovalDecision.APPROVED)

        task = asyncio.create_task(_resolve_soon())
        decision = await gate.request_approval(
            user_id="1",
            tool_name="test_tool",
            description="test description",
            publish_outbound=mock_publish,
            channel="telegram",
            chat_id="chat_1",
            timeout=5.0,
        )
        await task
        assert decision == ApprovalDecision.APPROVED
        assert not gate.has_pending("1")

    @pytest.mark.asyncio()
    async def test_timeout_returns_denied(self) -> None:
        gate = ApprovalGate()
        mock_publish = AsyncMock()

        decision = await gate.request_approval(
            user_id="1",
            tool_name="test_tool",
            description="test description",
            publish_outbound=mock_publish,
            channel="telegram",
            chat_id="chat_1",
            timeout=0.01,
        )
        assert decision == ApprovalDecision.DENIED
        assert not gate.has_pending("1")

    async def test_resolve_returns_false_when_nothing_pending(self) -> None:
        gate = ApprovalGate()
        assert await gate.resolve("999", ApprovalDecision.APPROVED) is False

    @pytest.mark.asyncio()
    async def test_request_approval_persists_row_and_cleans_up_on_resolve(
        self, test_user: User
    ) -> None:
        """An in-flight approval must leave a pending_approvals row so that a
        fresh worker can notify the user after a crash; resolving cleanly
        must delete the row so it does not look orphaned on next restart.
        """
        from backend.app.models import PendingApprovalRow

        gate = ApprovalGate()
        prompt_sent = asyncio.Event()
        release_publish = asyncio.Event()

        async def _publish(_msg: OutboundMessage) -> None:
            # request_approval() only calls publish_outbound after the
            # pending_approvals row has been persisted.
            prompt_sent.set()
            await release_publish.wait()

        request_task = asyncio.create_task(
            gate.request_approval(
                user_id=test_user.id,
                tool_name="write_file",
                description="write a file",
                publish_outbound=_publish,
                channel="telegram",
                chat_id="chat_1",
                timeout=5.0,
            )
        )
        await asyncio.wait_for(prompt_sent.wait(), timeout=1.0)

        async with db_session_async() as db:
            row = await db.get(PendingApprovalRow, test_user.id)
            assert row is not None, "row should exist while approval is in flight"
            assert row.tool_name == "write_file"
            assert row.channel == "telegram"

        release_publish.set()
        await gate.resolve(test_user.id, ApprovalDecision.APPROVED)
        decision = await asyncio.wait_for(request_task, timeout=1.0)
        assert decision == ApprovalDecision.APPROVED

        async with db_session_async() as db:
            assert await db.get(PendingApprovalRow, test_user.id) is None, (
                "row must be deleted once the approval resolves"
            )

    @pytest.mark.asyncio()
    async def test_cleanup_orphaned_approvals_notifies_and_clears(self, test_user: User) -> None:
        """On worker startup, every pending_approvals row (orphaned from a
        prior crash) gets a recovery message and is deleted."""
        from backend.app.agent.approval import cleanup_orphaned_approvals
        from backend.app.models import PendingApprovalRow

        async with db_session_async() as db:
            db.add(
                PendingApprovalRow(
                    user_id=test_user.id,
                    tool_name="write_file",
                    description="write a file",
                    channel="telegram",
                    chat_id="chat_99",
                )
            )
            await db.commit()

        published: list[OutboundMessage] = []

        async def _publish(msg: OutboundMessage) -> None:
            published.append(msg)

        recovered = await cleanup_orphaned_approvals(_publish)

        assert recovered == 1
        assert len(published) == 1
        assert published[0].chat_id == "chat_99"
        assert "interrupted" in published[0].content.lower()
        async with db_session_async() as db:
            assert await db.get(PendingApprovalRow, test_user.id) is None

    @pytest.mark.asyncio()
    async def test_cleanup_drops_malformed_rows_without_publishing(self, test_user: User) -> None:
        """Rows missing channel or chat_id cannot be delivered anywhere,
        so they should be deleted with a warning rather than left lingering."""
        from backend.app.agent.approval import cleanup_orphaned_approvals
        from backend.app.models import PendingApprovalRow

        async with db_session_async() as db:
            db.add(
                PendingApprovalRow(
                    user_id=test_user.id,
                    tool_name="write_file",
                    description="orphan with no channel",
                    channel="",
                    chat_id="",
                )
            )
            await db.commit()

        published: list[OutboundMessage] = []

        async def _publish(msg: OutboundMessage) -> None:
            published.append(msg)

        recovered = await cleanup_orphaned_approvals(_publish)

        assert recovered == 0
        assert published == [], "no message should be sent for a malformed row"
        async with db_session_async() as db:
            assert await db.get(PendingApprovalRow, test_user.id) is None

    @pytest.mark.asyncio()
    async def test_cleanup_drops_expired_rows_when_publish_fails(self, test_user: User) -> None:
        """If publish keeps failing on an orphan older than the TTL, the row
        must still be deleted so a permanently broken channel cannot keep
        the retry loop alive forever."""
        from datetime import UTC, datetime, timedelta

        from backend.app.agent.approval import cleanup_orphaned_approvals
        from backend.app.models import PendingApprovalRow

        async with db_session_async() as db:
            db.add(
                PendingApprovalRow(
                    user_id=test_user.id,
                    tool_name="write_file",
                    description="stale orphan",
                    channel="telegram",
                    chat_id="chat_expired",
                    created_at=datetime.now(UTC) - timedelta(days=1),
                )
            )
            await db.commit()

        async def _publish(_msg: OutboundMessage) -> None:
            raise RuntimeError("simulated channel failure")

        recovered = await cleanup_orphaned_approvals(_publish)

        assert recovered == 0
        async with db_session_async() as db:
            assert await db.get(PendingApprovalRow, test_user.id) is None

    @pytest.mark.asyncio()
    async def test_cleanup_keeps_fresh_rows_when_publish_fails(self, test_user: User) -> None:
        """A fresh orphan whose publish fails should stay in the table so a
        later restart can retry. Only expired rows are force-deleted."""
        from backend.app.agent.approval import cleanup_orphaned_approvals
        from backend.app.models import PendingApprovalRow

        async with db_session_async() as db:
            db.add(
                PendingApprovalRow(
                    user_id=test_user.id,
                    tool_name="write_file",
                    description="fresh orphan",
                    channel="telegram",
                    chat_id="chat_fresh",
                )
            )
            await db.commit()

        async def _publish(_msg: OutboundMessage) -> None:
            raise RuntimeError("transient channel failure")

        recovered = await cleanup_orphaned_approvals(_publish)

        assert recovered == 0
        async with db_session_async() as db:
            assert await db.get(PendingApprovalRow, test_user.id) is not None, (
                "fresh rows must survive a failed publish"
            )

    @pytest.mark.asyncio()
    async def test_resolve_deletes_row_before_waking_waiter(self, test_user: User) -> None:
        """resolve() must delete the pending_approvals row before event.set()
        so a crash between wake-up and the waiter's trailing cleanup can't
        leave an already-answered row to be mis-identified as an orphan."""
        from backend.app.models import PendingApprovalRow

        gate = ApprovalGate()
        prompt_sent = asyncio.Event()
        release_publish = asyncio.Event()

        async def _publish(_msg: OutboundMessage) -> None:
            prompt_sent.set()
            await release_publish.wait()

        request_task = asyncio.create_task(
            gate.request_approval(
                user_id=test_user.id,
                tool_name="write_file",
                description="write",
                publish_outbound=_publish,
                channel="telegram",
                chat_id="chat_1",
                timeout=5.0,
            )
        )
        await asyncio.wait_for(prompt_sent.wait(), timeout=1.0)

        async with db_session_async() as db:
            assert await db.get(PendingApprovalRow, test_user.id) is not None, (
                "row must exist before resolve deletes it"
            )

        release_publish.set()
        await gate.resolve(test_user.id, ApprovalDecision.APPROVED)

        # Immediately after resolve returns, the DB row must already be
        # gone, without waiting for request_approval's trailing cleanup.
        async with db_session_async() as db:
            assert await db.get(PendingApprovalRow, test_user.id) is None, (
                "resolve() must delete the row before waking the waiter"
            )

        decision = await asyncio.wait_for(request_task, timeout=1.0)
        assert decision == ApprovalDecision.APPROVED

    @pytest.mark.asyncio()
    async def test_persist_pending_row_upsert_is_idempotent(self, test_user: User) -> None:
        """_persist_pending_row uses ON CONFLICT DO UPDATE so repeated calls
        for the same user overwrite cleanly rather than racing a PK violation."""
        from backend.app.agent.approval import _persist_pending_row
        from backend.app.models import PendingApprovalRow

        await _persist_pending_row(test_user.id, "tool_a", "desc a", "telegram", "chat_1")
        await _persist_pending_row(test_user.id, "tool_b", "desc b", "bluebubbles", "chat_2")

        async with db_session_async() as db:
            row = await db.get(PendingApprovalRow, test_user.id)
            assert row is not None
            assert row.tool_name == "tool_b"
            assert row.channel == "bluebubbles"
            assert row.chat_id == "chat_2"

    @pytest.mark.asyncio()
    async def test_cleanup_skips_when_another_worker_holds_lock(
        self, test_user: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When a peer worker holds the cleanup advisory lock, this worker
        must return 0 and publish nothing so users don't receive duplicate
        'previous request was interrupted' messages during a rolling restart."""
        from backend.app.agent import approval as approval_module
        from backend.app.agent.approval import cleanup_orphaned_approvals
        from backend.app.models import PendingApprovalRow

        async with db_session_async() as db:
            db.add(
                PendingApprovalRow(
                    user_id=test_user.id,
                    tool_name="write_file",
                    description="would-be orphan",
                    channel="telegram",
                    chat_id="chat_lock",
                )
            )
            await db.commit()

        class _LockedAsyncSession:
            """AsyncSessionLocal stand-in where pg_try_advisory_lock returns False."""

            def __init__(self) -> None:
                self._closed = False

            async def execute(self, *_args: object, **_kwargs: object) -> Any:
                class _Result:
                    def scalar(self) -> bool:
                        return False

                return _Result()

            async def commit(self) -> None:
                pass

            async def close(self) -> None:
                self._closed = True

        monkeypatch.setattr(approval_module, "AsyncSessionLocal", _LockedAsyncSession)

        published: list[OutboundMessage] = []

        async def _publish(msg: OutboundMessage) -> None:
            published.append(msg)

        recovered = await cleanup_orphaned_approvals(_publish)

        assert recovered == 0
        assert published == [], (
            "no message should be sent when another worker owns the cleanup lock"
        )
        async with db_session_async() as db:
            assert await db.get(PendingApprovalRow, test_user.id) is not None, (
                "the peer worker that owns the lock must remain responsible for the row"
            )

    @pytest.mark.asyncio()
    async def test_has_pending(self) -> None:
        gate = ApprovalGate()
        assert not gate.has_pending("1")

        mock_publish = AsyncMock()

        async def _check_and_resolve() -> None:
            await _await_pending(gate, "1")
            assert gate.has_pending("1")
            await gate.resolve("1", ApprovalDecision.DENIED)

        task = asyncio.create_task(_check_and_resolve())
        await gate.request_approval(
            user_id="1",
            tool_name="t",
            description="d",
            publish_outbound=mock_publish,
            channel="telegram",
            chat_id="c",
            timeout=5.0,
        )
        await task

    @pytest.mark.asyncio()
    async def test_note_ambiguous_reply_returns_prompt_then_caps(self) -> None:
        """note_ambiguous_reply returns the prompt until the re-prompt cap."""
        gate = ApprovalGate()
        # No pending approval -> nothing to re-prompt.
        assert gate.note_ambiguous_reply("1") is None

        mock_publish = AsyncMock()

        async def _drive() -> None:
            await _await_pending(gate, "1")
            # First two ambiguous replies return the stored prompt to re-send.
            first = gate.note_ambiguous_reply("1")
            assert first is not None
            assert "yes" in first.lower()
            assert gate.note_ambiguous_reply("1") is not None
            # Third one is over the cap: caller should fall back to INTERRUPTED.
            assert gate.note_ambiguous_reply("1") is None
            # Gate is still pending the whole time -- re-prompting never resolves it.
            assert gate.has_pending("1")
            await gate.resolve("1", ApprovalDecision.APPROVED)

        task = asyncio.create_task(_drive())
        await gate.request_approval(
            user_id="1",
            tool_name="t",
            description="d",
            publish_outbound=mock_publish,
            channel="telegram",
            chat_id="c",
            timeout=5.0,
        )
        await task


# ---------------------------------------------------------------------------
# Agent integration
# ---------------------------------------------------------------------------


class TestAgentApproval:
    @pytest.mark.asyncio()
    @patch("backend.app.agent.core.amessages")
    async def test_tool_without_policy_executes_normally(
        self, mock_amessages: object, test_user: User
    ) -> None:
        """Tools without approval_policy execute unchanged."""
        tool = Tool(
            name="echo",
            description="Echo text",
            function=_echo_tool,
            params_model=_EchoParams,
        )
        mock_amessages.side_effect = [  # type: ignore[union-attr]
            make_tool_call_response([{"name": "echo", "arguments": {"text": "hello"}}]),
            make_text_response("Done!"),
        ]
        agent = ClawboltAgent(user=test_user)
        agent.register_tools([tool])
        response = await agent.process_message("echo hello")
        assert response.reply_text == "Done!"
        assert any(tc.name == "echo" and not tc.is_error for tc in response.tool_calls)

    @pytest.mark.asyncio()
    @patch("backend.app.agent.core.amessages")
    async def test_tool_with_auto_skips_gate(self, mock_amessages: object, test_user: User) -> None:
        """Tool with AUTO default_level executes without prompting."""
        tool = Tool(
            name="echo",
            description="Echo text",
            function=_echo_tool,
            params_model=_EchoParams,
            approval_policy=ApprovalPolicy(default_level=PermissionLevel.ALWAYS),
        )
        mock_amessages.side_effect = [  # type: ignore[union-attr]
            make_tool_call_response([{"name": "echo", "arguments": {"text": "hello"}}]),
            make_text_response("Done!"),
        ]
        agent = ClawboltAgent(user=test_user)
        agent.register_tools([tool])
        response = await agent.process_message("echo hello")
        assert any(tc.name == "echo" and not tc.is_error for tc in response.tool_calls)

    @pytest.mark.asyncio()
    @patch("backend.app.agent.core.amessages")
    async def test_tool_with_deny_returns_error(
        self, mock_amessages: object, test_user: User
    ) -> None:
        """Tool with DENY default_level returns a permission error."""
        tool = Tool(
            name="dangerous",
            description="Dangerous tool",
            function=_echo_tool,
            params_model=_EchoParams,
            approval_policy=ApprovalPolicy(default_level=PermissionLevel.NEVER),
        )
        mock_amessages.side_effect = [  # type: ignore[union-attr]
            make_tool_call_response([{"name": "dangerous", "arguments": {"text": "boom"}}]),
            make_text_response("Denied!"),
        ]
        agent = ClawboltAgent(user=test_user)
        agent.register_tools([tool])
        response = await agent.process_message("do it")
        assert any(tc.name == "dangerous" and tc.is_error for tc in response.tool_calls)

    @pytest.mark.asyncio()
    @patch("backend.app.agent.core.amessages")
    async def test_tool_with_ask_approved_executes(
        self, mock_amessages: object, test_user: User
    ) -> None:
        """Tool with ASK that gets approved executes."""
        mock_publish = AsyncMock()

        tool = Tool(
            name="fetcher",
            description="Fetch URL",
            function=_fetch_tool,
            params_model=_UrlParams,
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ASK,
                description_builder=_describe_fetch,
            ),
        )
        mock_amessages.side_effect = [  # type: ignore[union-attr]
            make_tool_call_response(
                [{"name": "fetcher", "arguments": {"url": "https://example.com"}}]
            ),
            make_text_response("Fetched!"),
        ]

        gate = get_approval_gate()

        async def _approve_soon() -> None:
            while not gate.has_pending(test_user.id):
                await asyncio.sleep(0.005)
            await gate.resolve(test_user.id, ApprovalDecision.APPROVED)

        agent = ClawboltAgent(
            user=test_user,
            channel="telegram",
            publish_outbound=mock_publish,
            chat_id="chat_1",
        )
        agent.register_tools([tool])

        task = asyncio.create_task(_approve_soon())
        response = await agent.process_message("fetch example.com")
        await task

        assert any(tc.name == "fetcher" and not tc.is_error for tc in response.tool_calls)
        mock_publish.assert_called()

    @pytest.mark.asyncio()
    @patch("backend.app.agent.core.amessages")
    async def test_tool_with_ask_denied_returns_error(
        self, mock_amessages: object, test_user: User
    ) -> None:
        """Tool with ASK that gets denied returns an error."""
        mock_publish = AsyncMock()

        tool = Tool(
            name="fetcher",
            description="Fetch URL",
            function=_fetch_tool,
            params_model=_UrlParams,
            approval_policy=ApprovalPolicy(default_level=PermissionLevel.ASK),
        )
        mock_amessages.side_effect = [  # type: ignore[union-attr]
            make_tool_call_response(
                [{"name": "fetcher", "arguments": {"url": "https://example.com"}}]
            ),
            make_text_response("Denied!"),
        ]

        gate = get_approval_gate()

        async def _deny_soon() -> None:
            while not gate.has_pending(test_user.id):
                await asyncio.sleep(0.005)
            await gate.resolve(test_user.id, ApprovalDecision.DENIED)

        agent = ClawboltAgent(
            user=test_user,
            channel="telegram",
            publish_outbound=mock_publish,
            chat_id="chat_1",
        )
        agent.register_tools([tool])

        task = asyncio.create_task(_deny_soon())
        response = await agent.process_message("fetch example.com")
        await task

        assert any(tc.name == "fetcher" and tc.is_error for tc in response.tool_calls)

    @pytest.mark.asyncio()
    @patch("backend.app.agent.core.amessages")
    async def test_always_persists_auto_to_store(
        self, mock_amessages: object, test_user: User
    ) -> None:
        """'always' decision persists AUTO to the approval store."""
        mock_publish = AsyncMock()

        tool = Tool(
            name="fetcher",
            description="Fetch URL",
            function=_fetch_tool,
            params_model=_UrlParams,
            approval_policy=ApprovalPolicy(default_level=PermissionLevel.ASK),
        )
        mock_amessages.side_effect = [  # type: ignore[union-attr]
            make_tool_call_response(
                [{"name": "fetcher", "arguments": {"url": "https://example.com"}}]
            ),
            make_text_response("Done!"),
        ]

        gate = get_approval_gate()

        async def _always_soon() -> None:
            while not gate.has_pending(test_user.id):
                await asyncio.sleep(0.005)
            await gate.resolve(test_user.id, ApprovalDecision.ALWAYS_ALLOW)

        agent = ClawboltAgent(
            user=test_user,
            channel="telegram",
            publish_outbound=mock_publish,
            chat_id="chat_1",
        )
        agent.register_tools([tool])

        task = asyncio.create_task(_always_soon())
        await agent.process_message("fetch example.com")
        await task

        store = get_approval_store()
        level = await store.check_permission(test_user.id, "fetcher")
        assert level == PermissionLevel.ALWAYS

    @pytest.mark.asyncio()
    @patch("backend.app.agent.core.amessages")
    async def test_always_allow_all_persists_tool_level_for_every_resource(
        self, mock_amessages: object, test_user: User
    ) -> None:
        """'always all' on a resource-scoped tool persists a tool-level ALWAYS.

        Regression for the QuickBooks invoice-send complaint (issue #1451):
        approving one recipient with the blanket option must auto-approve a
        DIFFERENT recipient too, instead of re-prompting per email.
        """
        mock_publish = AsyncMock()

        tool = Tool(
            name="fetcher",
            description="Fetch URL",
            function=_fetch_tool,
            params_model=_UrlParams,
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ASK,
                resource_extractor=_extract_domain,
                description_builder=_describe_fetch,
                resource_noun="domains",
            ),
        )
        mock_amessages.side_effect = [  # type: ignore[union-attr]
            make_tool_call_response(
                [{"name": "fetcher", "arguments": {"url": "https://example.com"}}]
            ),
            make_text_response("Done!"),
        ]

        gate = get_approval_gate()

        async def _always_all_soon() -> None:
            while not gate.has_pending(test_user.id):
                await asyncio.sleep(0.005)
            await gate.resolve(test_user.id, ApprovalDecision.ALWAYS_ALLOW_ALL)

        agent = ClawboltAgent(
            user=test_user,
            channel="telegram",
            publish_outbound=mock_publish,
            chat_id="chat_1",
        )
        agent.register_tools([tool])

        task = asyncio.create_task(_always_all_soon())
        await agent.process_message("fetch example.com")
        await task

        store = get_approval_store()
        # Stored at the tool level (resource=None), so a brand-new resource
        # the user never saw is already approved.
        assert await store.check_permission(test_user.id, "fetcher") == PermissionLevel.ALWAYS
        assert (
            await store.check_permission(test_user.id, "fetcher", resource="other.com")
            == PermissionLevel.ALWAYS
        )

    @pytest.mark.asyncio()
    @patch("backend.app.agent.core.amessages")
    async def test_always_allow_all_does_not_escalate_when_tool_opted_out(
        self, mock_amessages: object, test_user: User
    ) -> None:
        """A blanket decision must not grant more than the tool offered.

        The "always all" keywords resolve globally, but only tools that
        declare a resource_noun show the option. A resource-scoped tool that
        opted out (no resource_noun) must scope the grant to the shown
        resource, so a typed "allow all" cannot silently allow every target.
        """
        mock_publish = AsyncMock()

        tool = Tool(
            name="fetcher",
            description="Fetch URL",
            function=_fetch_tool,
            params_model=_UrlParams,
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ASK,
                resource_extractor=_extract_domain,
                description_builder=_describe_fetch,
                # No resource_noun: this tool never offers the blanket option.
            ),
        )
        mock_amessages.side_effect = [  # type: ignore[union-attr]
            make_tool_call_response(
                [{"name": "fetcher", "arguments": {"url": "https://example.com"}}]
            ),
            make_text_response("Done!"),
        ]

        gate = get_approval_gate()

        async def _always_all_soon() -> None:
            while not gate.has_pending(test_user.id):
                await asyncio.sleep(0.005)
            await gate.resolve(test_user.id, ApprovalDecision.ALWAYS_ALLOW_ALL)

        agent = ClawboltAgent(
            user=test_user,
            channel="telegram",
            publish_outbound=mock_publish,
            chat_id="chat_1",
        )
        agent.register_tools([tool])

        task = asyncio.create_task(_always_all_soon())
        await agent.process_message("fetch example.com")
        await task

        store = get_approval_store()
        # Scoped to the shown resource only; a different one still asks.
        assert (
            await store.check_permission(test_user.id, "fetcher", resource="example.com")
            == PermissionLevel.ALWAYS
        )
        assert (
            await store.check_permission(test_user.id, "fetcher", resource="other.com")
            == PermissionLevel.ASK
        )

    @pytest.mark.asyncio()
    @patch("backend.app.agent.core.amessages")
    async def test_always_allow_scopes_to_single_resource(
        self, mock_amessages: object, test_user: User
    ) -> None:
        """Contrast with the blanket option: plain 'always' on a resource-scoped
        tool only remembers the one resource, so a different one still asks."""
        mock_publish = AsyncMock()

        tool = Tool(
            name="fetcher",
            description="Fetch URL",
            function=_fetch_tool,
            params_model=_UrlParams,
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ASK,
                resource_extractor=_extract_domain,
                description_builder=_describe_fetch,
                resource_noun="domains",
            ),
        )
        mock_amessages.side_effect = [  # type: ignore[union-attr]
            make_tool_call_response(
                [{"name": "fetcher", "arguments": {"url": "https://example.com"}}]
            ),
            make_text_response("Done!"),
        ]

        gate = get_approval_gate()

        async def _always_soon() -> None:
            while not gate.has_pending(test_user.id):
                await asyncio.sleep(0.005)
            await gate.resolve(test_user.id, ApprovalDecision.ALWAYS_ALLOW)

        agent = ClawboltAgent(
            user=test_user,
            channel="telegram",
            publish_outbound=mock_publish,
            chat_id="chat_1",
        )
        agent.register_tools([tool])

        task = asyncio.create_task(_always_soon())
        await agent.process_message("fetch example.com")
        await task

        store = get_approval_store()
        assert (
            await store.check_permission(test_user.id, "fetcher", resource="example.com")
            == PermissionLevel.ALWAYS
        )
        # A different recipient/domain was never approved, so it still asks.
        assert (
            await store.check_permission(test_user.id, "fetcher", resource="other.com")
            == PermissionLevel.ASK
        )

    @pytest.mark.asyncio()
    @patch("backend.app.agent.core.amessages")
    async def test_never_persists_deny_to_store(
        self, mock_amessages: object, test_user: User
    ) -> None:
        """'never' decision persists DENY to the approval store."""
        mock_publish = AsyncMock()

        tool = Tool(
            name="fetcher",
            description="Fetch URL",
            function=_fetch_tool,
            params_model=_UrlParams,
            approval_policy=ApprovalPolicy(default_level=PermissionLevel.ASK),
        )
        mock_amessages.side_effect = [  # type: ignore[union-attr]
            make_tool_call_response(
                [{"name": "fetcher", "arguments": {"url": "https://example.com"}}]
            ),
            make_text_response("Blocked!"),
        ]

        gate = get_approval_gate()

        async def _never_soon() -> None:
            while not gate.has_pending(test_user.id):
                await asyncio.sleep(0.005)
            await gate.resolve(test_user.id, ApprovalDecision.ALWAYS_DENY)

        agent = ClawboltAgent(
            user=test_user,
            channel="telegram",
            publish_outbound=mock_publish,
            chat_id="chat_1",
        )
        agent.register_tools([tool])

        task = asyncio.create_task(_never_soon())
        await agent.process_message("fetch example.com")
        await task

        store = get_approval_store()
        level = await store.check_permission(test_user.id, "fetcher")
        assert level == PermissionLevel.NEVER

    @pytest.mark.asyncio()
    @patch("backend.app.agent.core.amessages")
    async def test_tool_with_ask_interrupted_returns_error(
        self, mock_amessages: object, test_user: User
    ) -> None:
        """Tool with ASK that gets INTERRUPTED returns an error with no permission persisted."""
        mock_publish = AsyncMock()

        tool = Tool(
            name="fetcher",
            description="Fetch URL",
            function=_fetch_tool,
            params_model=_UrlParams,
            approval_policy=ApprovalPolicy(default_level=PermissionLevel.ASK),
        )
        mock_amessages.side_effect = [  # type: ignore[union-attr]
            make_tool_call_response(
                [{"name": "fetcher", "arguments": {"url": "https://example.com"}}]
            ),
            make_text_response("OK, moving on."),
        ]

        gate = get_approval_gate()

        async def _interrupt_soon() -> None:
            while not gate.has_pending(test_user.id):
                await asyncio.sleep(0.005)
            await gate.resolve(test_user.id, ApprovalDecision.INTERRUPTED)

        agent = ClawboltAgent(
            user=test_user,
            channel="telegram",
            publish_outbound=mock_publish,
            chat_id="chat_1",
        )
        agent.register_tools([tool])

        task = asyncio.create_task(_interrupt_soon())
        response = await agent.process_message("fetch example.com")
        await task

        # Tool result should be an error with "interrupted" in the message
        assert any(
            tc.name == "fetcher" and tc.is_error and "interrupted" in tc.result.lower()
            for tc in response.tool_calls
        )
        # No permission should have been persisted
        store = get_approval_store()
        level = await store.check_permission(test_user.id, "fetcher")
        assert level == PermissionLevel.ASK  # unchanged from default

    @pytest.mark.asyncio()
    @patch("backend.app.agent.core.amessages")
    async def test_interrupted_does_not_persist_permission(
        self, mock_amessages: object, test_user: User
    ) -> None:
        """INTERRUPTED decision does not persist any permission override."""
        mock_publish = AsyncMock()

        tool = Tool(
            name="fetcher",
            description="Fetch URL",
            function=_fetch_tool,
            params_model=_UrlParams,
            approval_policy=ApprovalPolicy(
                default_level=PermissionLevel.ASK,
                resource_extractor=_extract_domain,
            ),
        )
        mock_amessages.side_effect = [  # type: ignore[union-attr]
            make_tool_call_response(
                [{"name": "fetcher", "arguments": {"url": "https://example.com"}}]
            ),
            make_text_response("Sure, what's up?"),
        ]

        gate = get_approval_gate()

        async def _interrupt_soon() -> None:
            while not gate.has_pending(test_user.id):
                await asyncio.sleep(0.005)
            await gate.resolve(test_user.id, ApprovalDecision.INTERRUPTED)

        agent = ClawboltAgent(
            user=test_user,
            channel="telegram",
            publish_outbound=mock_publish,
            chat_id="chat_1",
        )
        agent.register_tools([tool])

        task = asyncio.create_task(_interrupt_soon())
        await agent.process_message("fetch example.com")
        await task

        # Neither tool-level nor resource-level permission should be stored
        store = get_approval_store()
        data = await store.load_user_permissions(test_user.id)
        assert "fetcher" not in data.get("tools", {})
        assert "fetcher" not in data.get("resources", {})

    @pytest.mark.asyncio()
    @patch("backend.app.agent.core.amessages")
    async def test_stored_auto_skips_prompt(self, mock_amessages: object, test_user: User) -> None:
        """A stored AUTO permission skips the approval prompt entirely."""
        mock_publish = AsyncMock()

        store = get_approval_store()
        await store.set_permission(test_user.id, "fetcher", PermissionLevel.ALWAYS)

        tool = Tool(
            name="fetcher",
            description="Fetch URL",
            function=_fetch_tool,
            params_model=_UrlParams,
            approval_policy=ApprovalPolicy(default_level=PermissionLevel.ASK),
        )
        mock_amessages.side_effect = [  # type: ignore[union-attr]
            make_tool_call_response(
                [{"name": "fetcher", "arguments": {"url": "https://example.com"}}]
            ),
            make_text_response("Done!"),
        ]

        agent = ClawboltAgent(
            user=test_user,
            channel="telegram",
            publish_outbound=mock_publish,
            chat_id="chat_1",
        )
        agent.register_tools([tool])

        response = await agent.process_message("fetch example.com")
        assert any(tc.name == "fetcher" and not tc.is_error for tc in response.tool_calls)
        # publish_outbound should only be called for typing indicator, not approval prompt
        for call in mock_publish.call_args_list:
            msg = call.args[0] if call.args else call.kwargs.get("msg")
            if isinstance(msg, OutboundMessage):
                assert "wants to use" not in msg.content


# ---------------------------------------------------------------------------
# Ingestion intercept
# ---------------------------------------------------------------------------


class TestIngestionIntercept:
    @pytest.mark.asyncio()
    async def test_approval_response_resolves_gate(self, test_user: User) -> None:
        """An approval response resolves the gate and skips normal processing."""
        gate = get_approval_gate()

        mock_publish = AsyncMock()

        # Start a pending approval
        async def _start_approval() -> ApprovalDecision:
            return await gate.request_approval(
                user_id=test_user.id,
                tool_name="test_tool",
                description="test",
                publish_outbound=mock_publish,
                channel="telegram",
                chat_id="chat_1",
                timeout=5.0,
            )

        approval_task = asyncio.create_task(_start_approval())
        await _await_pending(gate, test_user.id)
        assert gate.has_pending(test_user.id)

        # Simulate inbound "yes" message
        inbound = InboundMessage(
            channel="telegram",
            sender_id=str(test_user.id),
            text="yes",
        )

        with patch(
            "backend.app.agent.ingestion._get_or_create_user",
            new_callable=AsyncMock,
            return_value=test_user,
        ):
            await process_inbound_from_bus(inbound)

        decision = await approval_task
        assert decision == ApprovalDecision.APPROVED
        assert not gate.has_pending(test_user.id)

    @pytest.mark.asyncio()
    async def test_non_approval_text_interrupts_gate(self, test_user: User) -> None:
        """Unrelated text while pending resolves the gate as INTERRUPTED."""
        gate = get_approval_gate()

        mock_publish = AsyncMock()

        async def _start_approval() -> ApprovalDecision:
            return await gate.request_approval(
                user_id=test_user.id,
                tool_name="test_tool",
                description="test",
                publish_outbound=mock_publish,
                channel="telegram",
                chat_id="chat_1",
                timeout=5.0,
            )

        approval_task = asyncio.create_task(_start_approval())
        await _await_pending(gate, test_user.id)
        assert gate.has_pending(test_user.id)

        inbound = InboundMessage(
            channel="telegram",
            sender_id=str(test_user.id),
            text="what is the weather?",
        )

        mock_batcher = AsyncMock()
        with (
            patch(
                "backend.app.agent.ingestion._get_or_create_user",
                new_callable=AsyncMock,
                return_value=test_user,
            ),
            patch(
                "backend.app.agent.ingestion.classify_approval_response",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "backend.app.agent.ingestion.message_batcher",
                mock_batcher,
            ),
        ):
            await process_inbound_from_bus(inbound)

        decision = await approval_task
        assert decision == ApprovalDecision.INTERRUPTED
        assert not gate.has_pending(test_user.id)

    @pytest.mark.asyncio()
    async def test_ambiguous_reply_reprompts_and_keeps_gate_pending(self, test_user: User) -> None:
        """Filler like "lol" re-prompts without interrupting the batch.

        Regression for the calendar partial-apply bug: an ambiguous reply
        during a batched approval used to resolve the gate as INTERRUPTED,
        aborting the rest of the batch. It should instead re-prompt for a
        clear yes/no and leave the gate pending.
        """
        gate = get_approval_gate()
        mock_publish = AsyncMock()

        async def _start_approval() -> ApprovalDecision:
            return await gate.request_approval(
                user_id=test_user.id,
                tool_name="calendar_update_event",
                description="Update calendar event: Day 4",
                publish_outbound=mock_publish,
                channel="telegram",
                chat_id="chat_1",
                timeout=5.0,
            )

        approval_task = asyncio.create_task(_start_approval())
        await _await_pending(gate, test_user.id)

        inbound = InboundMessage(
            channel="telegram",
            sender_id=str(test_user.id),
            text="lol",
        )

        mock_batcher = AsyncMock()
        with (
            patch(
                "backend.app.agent.ingestion._get_or_create_user",
                new_callable=AsyncMock,
                return_value=test_user,
            ),
            patch(
                "backend.app.agent.ingestion.classify_approval_response",
                new_callable=AsyncMock,
                return_value=AMBIGUOUS_APPROVAL_REPLY,
            ),
            patch(
                "backend.app.agent.ingestion.message_batcher",
                mock_batcher,
            ),
            patch.object(message_bus, "publish_outbound", new_callable=AsyncMock) as mock_out,
        ):
            await process_inbound_from_bus(inbound)

            # The gate stays pending: the blocked agent loop is still waiting.
            assert gate.has_pending(test_user.id)
            # The user got a re-prompt asking for a clear yes/no.
            mock_out.assert_called_once()
            sent: OutboundMessage = mock_out.call_args.args[0]
            assert sent.content.startswith("Sorry, I didn't catch that")
            assert "yes" in sent.content.lower()
            # The filler message was NOT dispatched to the pipeline.
            mock_batcher.enqueue.assert_not_called()

            # A clear "yes" now resolves the still-pending gate.
            await gate.resolve(test_user.id, ApprovalDecision.APPROVED)

        decision = await approval_task
        assert decision == ApprovalDecision.APPROVED
        assert not gate.has_pending(test_user.id)

    @pytest.mark.asyncio()
    async def test_repeated_ambiguous_replies_eventually_interrupt(self, test_user: User) -> None:
        """After the re-prompt cap, an ambiguous reply falls back to INTERRUPTED."""
        gate = get_approval_gate()
        mock_publish = AsyncMock()

        async def _start_approval() -> ApprovalDecision:
            return await gate.request_approval(
                user_id=test_user.id,
                tool_name="calendar_update_event",
                description="Update calendar event: Day 4",
                publish_outbound=mock_publish,
                channel="telegram",
                chat_id="chat_1",
                timeout=5.0,
            )

        approval_task = asyncio.create_task(_start_approval())
        await _await_pending(gate, test_user.id)

        inbound = InboundMessage(
            channel="telegram",
            sender_id=str(test_user.id),
            text="haha",
        )

        mock_batcher = AsyncMock()
        with (
            patch(
                "backend.app.agent.ingestion._get_or_create_user",
                new_callable=AsyncMock,
                return_value=test_user,
            ),
            patch(
                "backend.app.agent.ingestion.classify_approval_response",
                new_callable=AsyncMock,
                return_value=AMBIGUOUS_APPROVAL_REPLY,
            ),
            patch(
                "backend.app.agent.ingestion.message_batcher",
                mock_batcher,
            ),
            patch.object(message_bus, "publish_outbound", new_callable=AsyncMock),
        ):
            # First two ambiguous replies re-prompt; the gate stays pending.
            await process_inbound_from_bus(inbound)
            await process_inbound_from_bus(inbound)
            assert gate.has_pending(test_user.id)
            # The third one exceeds the cap and interrupts the batch instead.
            await process_inbound_from_bus(inbound)

        decision = await approval_task
        assert decision == ApprovalDecision.INTERRUPTED
        assert not gate.has_pending(test_user.id)
        # The interrupting message falls through to the pipeline.
        mock_batcher.enqueue.assert_called_once()

    @pytest.mark.asyncio()
    async def test_message_with_attachments_is_not_eaten_as_approval(self, test_user: User) -> None:
        """A message with media attachments bypasses approval-eating.

        A photo with caption "yes" carries payload the agent must act
        on. Resolving the gate as APPROVED and dropping the body
        silently loses the photo from session history. A bulk photo
        send with a caption like "yes" or "ok" that arrives during a
        pending approval would otherwise vanish. The fix: media-bearing
        messages fall through to the normal pipeline, which resolves
        any pending gate as INTERRUPTED rather than consuming the body.
        """
        gate = get_approval_gate()
        mock_publish = AsyncMock()

        async def _start_approval() -> ApprovalDecision:
            return await gate.request_approval(
                user_id=test_user.id,
                tool_name="test_tool",
                description="test",
                publish_outbound=mock_publish,
                channel="bluebubbles",
                chat_id="+15555550100",
                timeout=5.0,
            )

        approval_task = asyncio.create_task(_start_approval())
        await _await_pending(gate, test_user.id)
        assert gate.has_pending(test_user.id)

        # Caption is literally "yes" but the inbound carries 11 photos --
        # so it must NOT be treated as a bare approval reply.
        inbound = InboundMessage(
            channel="bluebubbles",
            sender_id=str(test_user.id),
            text="yes",
            media_refs=[(f"att_{i}", "image/jpeg") for i in range(11)],
            external_message_id="bb_test_with_media",
        )

        mock_batcher = AsyncMock()
        with (
            patch(
                "backend.app.agent.ingestion._get_or_create_user",
                new_callable=AsyncMock,
                return_value=test_user,
            ),
            patch(
                "backend.app.agent.ingestion.message_batcher",
                mock_batcher,
            ),
        ):
            await process_inbound_from_bus(inbound)

        await approval_task
        # The media-bearing message went to the pipeline, not the gate.
        mock_batcher.enqueue.assert_called_once()

    @pytest.mark.asyncio()
    async def test_interrupted_message_dispatched_to_pipeline(self, test_user: User) -> None:
        """Unrelated message during approval is dispatched to the pipeline."""
        gate = get_approval_gate()
        mock_publish = AsyncMock()

        async def _start_approval() -> ApprovalDecision:
            return await gate.request_approval(
                user_id=test_user.id,
                tool_name="test_tool",
                description="test",
                publish_outbound=mock_publish,
                channel="telegram",
                chat_id="chat_1",
                timeout=5.0,
            )

        approval_task = asyncio.create_task(_start_approval())
        await _await_pending(gate, test_user.id)

        inbound = InboundMessage(
            channel="telegram",
            sender_id=str(test_user.id),
            text="what is my schedule?",
        )

        mock_batcher = AsyncMock()
        with (
            patch(
                "backend.app.agent.ingestion._get_or_create_user",
                new_callable=AsyncMock,
                return_value=test_user,
            ),
            patch(
                "backend.app.agent.ingestion.classify_approval_response",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "backend.app.agent.ingestion.message_batcher",
                mock_batcher,
            ),
        ):
            await process_inbound_from_bus(inbound)

        await approval_task
        # The message should have been enqueued for pipeline processing
        mock_batcher.enqueue.assert_called_once()

    @pytest.mark.asyncio()
    async def test_llm_classified_approval_resolves_gate(self, test_user: User) -> None:
        """LLM-classified natural-language approval resolves the gate."""
        gate = get_approval_gate()

        mock_publish = AsyncMock()

        async def _start_approval() -> ApprovalDecision:
            return await gate.request_approval(
                user_id=test_user.id,
                tool_name="test_tool",
                description="test",
                publish_outbound=mock_publish,
                channel="telegram",
                chat_id="chat_1",
                timeout=5.0,
            )

        approval_task = asyncio.create_task(_start_approval())
        await _await_pending(gate, test_user.id)
        assert gate.has_pending(test_user.id)

        # "Yes to both" is not an exact match, but the LLM classifies it
        inbound = InboundMessage(
            channel="telegram",
            sender_id=str(test_user.id),
            text="Yes to both",
        )

        with (
            patch(
                "backend.app.agent.ingestion._get_or_create_user",
                new_callable=AsyncMock,
                return_value=test_user,
            ),
            patch(
                "backend.app.agent.ingestion.classify_approval_response",
                new_callable=AsyncMock,
                return_value=ApprovalDecision.APPROVED,
            ),
        ):
            await process_inbound_from_bus(inbound)

        decision = await approval_task
        assert decision == ApprovalDecision.APPROVED
        assert not gate.has_pending(test_user.id)

    @pytest.mark.asyncio()
    async def test_dispatch_resolves_stale_gate_while_waiting_for_lock(
        self, test_user: User
    ) -> None:
        """_dispatch_to_pipeline resolves a stale approval gate set up after the lock was taken."""
        from backend.app.agent.dto import SessionState, StoredMessage

        gate = get_approval_gate()
        mock_publish = AsyncMock()

        # Simulate pipeline 1 holding the lock and setting up a gate
        lock = user_locks.acquire(test_user.id)
        await lock.acquire()

        async def _setup_gate_then_release() -> ApprovalDecision:
            """Mimic pipeline 1: set up approval gate while holding the lock."""
            await asyncio.sleep(0.1)
            decision = await gate.request_approval(
                user_id=test_user.id,
                tool_name="calendar_read",
                description="Read calendar events",
                publish_outbound=mock_publish,
                channel="telegram",
                chat_id="chat_1",
                timeout=30.0,
            )
            # Pipeline 1 finishes after gate resolves
            lock.release()
            return decision

        gate_task = asyncio.create_task(_setup_gate_then_release())

        # Pipeline 2: dispatch a new message. The background poller should
        # resolve the gate so pipeline 1 releases the lock.
        session = SessionState(session_id="test", user_id=test_user.id)
        message = StoredMessage(direction="inbound", body="what's in quickbooks", seq=2)

        with patch("backend.app.agent.ingestion.handle_inbound_message", new_callable=AsyncMock):
            await asyncio.wait_for(
                _dispatch_to_pipeline(
                    user=test_user,
                    session=session,
                    message=message,
                    media_urls=[],
                    channel="telegram",
                ),
                timeout=5.0,
            )

        decision = await gate_task
        assert decision == ApprovalDecision.INTERRUPTED

    @pytest.mark.asyncio()
    async def test_dispatch_reloads_session_after_lock(self, test_user: User) -> None:
        """_dispatch_to_pipeline reloads session from DB after acquiring the user lock."""
        from backend.app.agent.dto import SessionState, StoredMessage

        session = SessionState(session_id="test-sess", user_id=test_user.id)
        message = StoredMessage(direction="inbound", body="hello", seq=1)

        fresh_session = SessionState(session_id="test-sess", user_id=test_user.id)
        fresh_session.messages = [
            StoredMessage(direction="inbound", body="hello", seq=1),
            StoredMessage(direction="outbound", body="tool result from pipeline 1", seq=2),
        ]

        from unittest.mock import MagicMock

        mock_store = MagicMock()
        mock_store.load_session_async = AsyncMock(return_value=fresh_session)

        mock_handle = AsyncMock()

        with (
            patch(
                "backend.app.agent.ingestion.handle_inbound_message",
                mock_handle,
            ),
            patch(
                "backend.app.agent.ingestion.get_session_store",
                return_value=mock_store,
            ),
        ):
            await _dispatch_to_pipeline(
                user=test_user,
                session=session,
                message=message,
                media_urls=[],
                channel="telegram",
            )

        # Session store should have been called to reload
        mock_store.load_session_async.assert_called_once_with("test-sess")

        # handle_inbound_message should have received the fresh session
        mock_handle.assert_called_once()
        call_kwargs = mock_handle.call_args.kwargs
        passed_session = call_kwargs["session"]
        assert len(passed_session.messages) == 2
        assert passed_session.messages[1].body == "tool result from pipeline 1"


# ---------------------------------------------------------------------------
# Module-level accessors
# ---------------------------------------------------------------------------


class TestModuleAccessors:
    def test_get_approval_gate_returns_singleton(self) -> None:
        g1 = get_approval_gate()
        g2 = get_approval_gate()
        assert g1 is g2

    def test_get_approval_store_returns_singleton(self) -> None:
        s1 = get_approval_store()
        s2 = get_approval_store()
        assert s1 is s2

    def test_reset_clears_singletons(self) -> None:
        g1 = get_approval_gate()
        s1 = get_approval_store()
        reset_approval_gate()
        g2 = get_approval_gate()
        s2 = get_approval_store()
        assert g1 is not g2
        assert s1 is not s2


# ---------------------------------------------------------------------------
# classify_approval_response: LLM call shape
# ---------------------------------------------------------------------------


class TestClassifyApprovalResponseCallShape:
    """Regression for prod 400 ``temperature is deprecated for this model``.

    The classifier called acompletion with ``temperature=0`` against
    claude-opus-4-7, which rejects that parameter. Every fuzzy approval
    response then fell through to the WARNING + INTERRUPTED fallback.
    """

    @pytest.mark.asyncio()
    async def test_acompletion_called_without_temperature(self) -> None:
        from pydantic import BaseModel as _BaseModel

        class _Parsed(_BaseModel):
            decision: str = "approved"

        mock_msg = AsyncMock()
        mock_msg.parsed = _Parsed()
        mock_choice = AsyncMock()
        mock_choice.message = mock_msg
        mock_response = AsyncMock()
        mock_response.choices = [mock_choice]

        with patch(
            "backend.app.agent.approval.acompletion",
            new=AsyncMock(return_value=mock_response),
        ) as mock_acompletion:
            await classify_approval_response("sure thing")

        kwargs = mock_acompletion.call_args.kwargs
        assert "temperature" not in kwargs, (
            f"temperature must not be passed (claude-opus-4-7 rejects it): {kwargs}"
        )
        assert "response_format" in kwargs, (
            "response_format is what actually constrains the output to the enum"
        )

    @pytest.mark.asyncio()
    @pytest.mark.parametrize(
        ("decision", "expected"),
        [
            ("approved", ApprovalDecision.APPROVED),
            ("denied", ApprovalDecision.DENIED),
            ("always_allow", ApprovalDecision.ALWAYS_ALLOW),
            ("always_allow_all", ApprovalDecision.ALWAYS_ALLOW_ALL),
            ("always_deny", ApprovalDecision.ALWAYS_DENY),
            ("ambiguous", AMBIGUOUS_APPROVAL_REPLY),
            ("unrelated", None),
        ],
    )
    async def test_classification_maps_to_expected_result(
        self, decision: str, expected: object
    ) -> None:
        """Each classifier label maps to its decision; 'ambiguous' re-prompts."""
        from pydantic import BaseModel as _BaseModel

        class _Parsed(_BaseModel):
            decision: str

        mock_msg = AsyncMock()
        mock_msg.parsed = _Parsed(decision=decision)
        mock_choice = AsyncMock()
        mock_choice.message = mock_msg
        mock_response = AsyncMock()
        mock_response.choices = [mock_choice]

        with patch(
            "backend.app.agent.approval.acompletion",
            new=AsyncMock(return_value=mock_response),
        ):
            result = await classify_approval_response("lol")

        assert result == expected


class TestApprovalEvents:
    """The audit log appends one row per lifecycle transition so admins
    can replay what happened to an approval (requested, decided,
    timed_out, recovered) instead of seeing the prompt text only.
    """

    @pytest.mark.asyncio()
    async def test_requested_and_decided_pair_logged(self, test_user: User) -> None:
        from sqlalchemy import select

        from backend.app.agent.approval import get_approval_event_store
        from backend.app.models import ApprovalEvent

        gate = ApprovalGate()
        mock_publish = AsyncMock()

        async def _resolve_soon() -> None:
            await _await_pending(gate, test_user.id)
            await gate.resolve(test_user.id, ApprovalDecision.APPROVED)

        task = asyncio.create_task(_resolve_soon())
        await gate.request_approval(
            user_id=test_user.id,
            tool_name="write_file",
            description="write a file",
            publish_outbound=mock_publish,
            channel="telegram",
            chat_id="chat_1",
            timeout=5.0,
        )
        await task

        async with db_session_async() as db:
            rows = (
                (
                    await db.execute(
                        select(ApprovalEvent)
                        .filter(ApprovalEvent.user_id == test_user.id)
                        .order_by(ApprovalEvent.id.asc())
                    )
                )
                .scalars()
                .all()
            )
        assert [r.event_type for r in rows] == ["requested", "decided"]
        assert rows[0].tool_name == "write_file"
        assert rows[0].description == "write a file"
        assert rows[0].channel == "telegram"
        assert rows[0].chat_id == "chat_1"
        assert rows[0].decision is None
        assert rows[1].decision == "approved"

        # Read-side store returns them in chronological order.
        events = await get_approval_event_store().list_for_user(test_user.id)
        assert [e.event_type for e in events] == ["requested", "decided"]
        assert events[1].decision == "approved"

    @pytest.mark.asyncio()
    async def test_timeout_logs_timed_out_event(self, test_user: User) -> None:
        from sqlalchemy import select

        from backend.app.models import ApprovalEvent

        gate = ApprovalGate()
        mock_publish = AsyncMock()

        decision = await gate.request_approval(
            user_id=test_user.id,
            tool_name="write_file",
            description="write a file",
            publish_outbound=mock_publish,
            channel="telegram",
            chat_id="chat_1",
            timeout=0.01,
        )
        assert decision == ApprovalDecision.DENIED

        async with db_session_async() as db:
            rows = (
                (
                    await db.execute(
                        select(ApprovalEvent)
                        .filter(ApprovalEvent.user_id == test_user.id)
                        .order_by(ApprovalEvent.id.asc())
                    )
                )
                .scalars()
                .all()
            )
        assert [r.event_type for r in rows] == ["requested", "timed_out"]
        # No `decided` row on timeout: the gate never received a decision.
        assert all(r.decision is None for r in rows)

    @pytest.mark.asyncio()
    async def test_resolve_records_interrupted_decision(self, test_user: User) -> None:
        from sqlalchemy import select

        from backend.app.models import ApprovalEvent

        gate = ApprovalGate()
        mock_publish = AsyncMock()

        async def _interrupt_soon() -> None:
            await _await_pending(gate, test_user.id)
            await gate.resolve(test_user.id, ApprovalDecision.INTERRUPTED)

        task = asyncio.create_task(_interrupt_soon())
        await gate.request_approval(
            user_id=test_user.id,
            tool_name="discard_media",
            description="discard staged media",
            publish_outbound=mock_publish,
            channel="telegram",
            chat_id="chat_2",
            timeout=5.0,
        )
        await task

        async with db_session_async() as db:
            rows = (
                (
                    await db.execute(
                        select(ApprovalEvent)
                        .filter(ApprovalEvent.user_id == test_user.id)
                        .order_by(ApprovalEvent.id.asc())
                    )
                )
                .scalars()
                .all()
            )
        assert rows[-1].event_type == "decided"
        assert rows[-1].decision == "interrupted"

    @pytest.mark.asyncio()
    async def test_recovered_event_logged_on_orphan_cleanup(self, test_user: User) -> None:
        from sqlalchemy import select

        from backend.app.agent.approval import cleanup_orphaned_approvals
        from backend.app.models import ApprovalEvent, PendingApprovalRow

        async with db_session_async() as db:
            db.add(
                PendingApprovalRow(
                    user_id=test_user.id,
                    tool_name="write_file",
                    description="write a file",
                    channel="telegram",
                    chat_id="chat_99",
                )
            )
            await db.commit()

        async def _publish(msg: OutboundMessage) -> None:
            return None

        recovered = await cleanup_orphaned_approvals(_publish)
        assert recovered == 1

        async with db_session_async() as db:
            rows = (
                (
                    await db.execute(
                        select(ApprovalEvent)
                        .filter(ApprovalEvent.user_id == test_user.id)
                        .order_by(ApprovalEvent.id.asc())
                    )
                )
                .scalars()
                .all()
            )
        assert [r.event_type for r in rows] == ["recovered"]
        assert rows[0].tool_name == "write_file"
        assert rows[0].channel == "telegram"
        assert rows[0].chat_id == "chat_99"

    @pytest.mark.asyncio()
    async def test_event_store_respects_since_and_limit(self, test_user: User) -> None:
        from datetime import UTC, datetime, timedelta

        from backend.app.agent.approval import get_approval_event_store
        from backend.app.models import ApprovalEvent

        old = datetime.now(UTC) - timedelta(hours=2)
        recent = datetime.now(UTC)
        async with db_session_async() as db:
            db.add_all(
                [
                    ApprovalEvent(
                        user_id=test_user.id,
                        event_type="requested",
                        tool_name="t",
                        description="",
                        channel="",
                        chat_id="",
                        created_at=old,
                    ),
                    ApprovalEvent(
                        user_id=test_user.id,
                        event_type="decided",
                        tool_name="t",
                        description="",
                        channel="",
                        chat_id="",
                        decision="approved",
                        created_at=recent,
                    ),
                ]
            )
            await db.commit()

        store = get_approval_event_store()
        only_recent = await store.list_for_user(
            test_user.id, since=datetime.now(UTC) - timedelta(minutes=5)
        )
        assert [e.event_type for e in only_recent] == ["decided"]

        capped = await store.list_for_user(test_user.id, limit=1)
        assert len(capped) == 1
