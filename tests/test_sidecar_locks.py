"""Tests for the sidecar's per-site locking and stale-session recovery.

The sidecar lives outside the installed package because it carries the browser
stack, and it was previously assumed untestable for that reason. It is not:
stubbing ``patchright.async_api`` in ``sys.modules`` is enough to import it with
no browser and no extra dependency. That assumption is what let a transposed
lock mapping ship, so the mapping is asserted here directly.

Two properties matter, and one of them is not obvious:

* Requests for one retailer must serialize against each other, because they
  drive that retailer's single page.
* Requests for different retailers must not, or the Lowe's pre-warm (a homepage
  load, a click, and two settle waits) blocks every Home Depot request for
  roughly 20 seconds right after startup.
"""

import ast
import asyncio
import sys
import time
import types
from pathlib import Path
from typing import Any

import pytest

_SIDECAR_DIR = Path(__file__).resolve().parents[1] / "sidecar" / "home_depot"


def _load_sidecar() -> Any:
    """Import the sidecar module with the browser stack stubbed out."""
    stub_root = types.ModuleType("patchright")
    stub_api = types.ModuleType("patchright.async_api")
    stub_api.async_playwright = lambda: None  # type: ignore[attr-defined]
    stub_root.async_api = stub_api  # type: ignore[attr-defined]

    saved = {k: sys.modules.get(k) for k in ("patchright", "patchright.async_api")}
    sys.modules["patchright"] = stub_root
    sys.modules["patchright.async_api"] = stub_api
    sys.path.insert(0, str(_SIDECAR_DIR))
    try:
        import importlib

        module = importlib.import_module("sidecar")
        return importlib.reload(module)
    finally:
        sys.path.remove(str(_SIDECAR_DIR))
        for key, value in saved.items():
            if value is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value


sidecar = _load_sidecar()


# Request paths take their lock through `_timed_lock`, which adds the deadline
# and the timing log; the background pre-warm still takes it directly. Both name
# their retailer as a literal first argument, which is the property under test.
_LOCK_TAKING_CALLS = ("_lock_for", "_timed_lock")


def _lock_site_by_function() -> dict[str, str]:
    """Read which site each method locks, straight from the source.

    Parsed rather than executed so the assertion cannot be satisfied by a mock:
    the previous defect was a literal string in the wrong function body.
    """
    tree = ast.parse((_SIDECAR_DIR / "sidecar.py").read_text())
    found: dict[str, str] = {}

    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        for inner in ast.walk(node):
            call = None
            if isinstance(inner, ast.Call):
                call = inner
            if call is None or not isinstance(call.func, ast.Attribute):
                continue
            if call.func.attr not in _LOCK_TAKING_CALLS or not call.args:
                continue
            arg = call.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                found[node.name] = arg.value
    return found


class TestLockMapping:
    def test_each_method_locks_its_own_retailer(self) -> None:
        """A transposed mapping shipped once; assert it explicitly."""
        assert _lock_site_by_function() == {
            "_prewarm_lowes": "lowes",
            "search_lowes": "lowes",
            "search": "home_depot",
            "find_stores": "home_depot",
        }

    def test_lock_for_returns_a_stable_lock_per_site(self) -> None:
        engine = sidecar.BrowserBackedSearch()
        assert engine._lock_for("lowes") is engine._lock_for("lowes")
        assert engine._lock_for("lowes") is not engine._lock_for("home_depot")


class TestLockIsolation:
    @pytest.mark.asyncio
    async def test_one_retailer_does_not_block_the_other(self) -> None:
        """The Lowe's warm must not hold up Home Depot; that regression shipped."""
        engine = sidecar.BrowserBackedSearch()
        released = asyncio.Event()

        async def hold_lowes() -> None:
            async with engine._lock_for("lowes"):
                await released.wait()

        holder = asyncio.create_task(hold_lowes())
        await asyncio.sleep(0)  # let it take the lock

        # Home Depot's lock must be free while Lowe's is held.
        await asyncio.wait_for(engine._lock_for("home_depot").acquire(), timeout=0.5)
        engine._lock_for("home_depot").release()

        released.set()
        await holder

    @pytest.mark.asyncio
    async def test_same_retailer_still_serializes(self) -> None:
        """Both Home Depot entry points drive one page, so they must exclude."""
        engine = sidecar.BrowserBackedSearch()
        async with engine._lock_for("home_depot"):
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(engine._lock_for("home_depot").acquire(), timeout=0.1)


class TestStaleSessionRecovery:
    """A warmed Lowe's page can go stale; it must not pin every later search."""

    @staticmethod
    def _engine_with_page() -> tuple[Any, Any]:
        engine = sidecar.BrowserBackedSearch()
        closed: list[bool] = []

        class FakePage:
            async def close(self) -> None:
                closed.append(True)

        page = FakePage()
        engine._site_pages["lowes"] = page
        return engine, closed

    @pytest.mark.asyncio
    async def test_one_failure_keeps_the_session(self) -> None:
        """A re-warm costs ~20s, so a lone transient failure should not trigger it."""
        engine, closed = self._engine_with_page()

        await engine._note_lowes_failure()

        assert "lowes" in engine._site_pages
        assert closed == []

    @pytest.mark.asyncio
    async def test_two_consecutive_failures_discard_the_session(self) -> None:
        engine, closed = self._engine_with_page()

        await engine._note_lowes_failure()
        await engine._note_lowes_failure()

        assert "lowes" not in engine._site_pages, "a stale page must not be reused"
        assert closed == [True], "the discarded page must be closed, not leaked"

    @pytest.mark.asyncio
    async def test_the_counter_resets_after_discarding(self) -> None:
        """Otherwise the next single failure would discard a freshly warmed page."""
        engine, _ = self._engine_with_page()
        await engine._note_lowes_failure()
        await engine._note_lowes_failure()
        assert engine._lowes_failures == 0

    @pytest.mark.asyncio
    async def test_discarding_survives_a_page_that_fails_to_close(self) -> None:
        engine = sidecar.BrowserBackedSearch()

        class ExplodingPage:
            async def close(self) -> None:
                raise RuntimeError("browser already gone")

        engine._site_pages["lowes"] = ExplodingPage()
        await engine._note_lowes_failure()
        await engine._note_lowes_failure()

        assert "lowes" not in engine._site_pages


class TestRequestBudget:
    """A retailer that goes quiet must not park the lock (issue #1496).

    The in-page fetch had no abort signal and `page.evaluate` takes no timeout,
    so a connection that was accepted and never answered left the coroutine
    pending forever while holding the retailer's lock. Everything queued behind
    it then timed out client-side waiting for a page nobody was driving.
    """

    @staticmethod
    def _hanging_page() -> Any:
        class HangingPage:
            async def evaluate(self, _script: str, _args: list) -> dict:
                await asyncio.Event().wait()  # never resolves, like the real hang
                raise AssertionError("unreachable")

        return HangingPage()

    @pytest.mark.asyncio
    async def test_a_hung_page_gives_up_instead_of_pending_forever(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sidecar, "_EVALUATE_GRACE_SECONDS", 0.05)
        engine = sidecar.BrowserBackedSearch()

        with pytest.raises(sidecar.HTTPException) as caught:
            await engine._evaluate(
                self._hanging_page(),
                "script",
                ["arg"],
                what="Home Depot search",
                deadline=time.monotonic() + 0.1,
            )

        assert caught.value.status_code == 504

    @pytest.mark.asyncio
    async def test_a_hung_request_releases_the_lock_for_the_next_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The property that actually mattered: one bad request, not a cascade."""
        monkeypatch.setattr(sidecar, "_EVALUATE_GRACE_SECONDS", 0.05)
        monkeypatch.setattr(sidecar, "REQUEST_BUDGET_SECONDS", 0.1)
        engine = sidecar.BrowserBackedSearch()
        page = self._hanging_page()

        async def one_request() -> None:
            async with engine._timed_lock("home_depot", "hd") as deadline:
                await engine._evaluate(page, "s", [], what="hd", deadline=deadline)

        with pytest.raises(sidecar.HTTPException):
            await one_request()

        # The lock has to be free the instant the first request gives up.
        await asyncio.wait_for(engine._lock_for("home_depot").acquire(), timeout=0.5)

    @pytest.mark.asyncio
    async def test_an_expired_budget_is_refused_rather_than_extended(self) -> None:
        """The redirect retry shares one budget; it cannot mint a fresh slice."""
        engine = sidecar.BrowserBackedSearch()

        with pytest.raises(sidecar.HTTPException) as caught:
            await engine._evaluate(
                self._hanging_page(),
                "script",
                [],
                what="Home Depot search",
                deadline=time.monotonic() - 1,
            )

        assert caught.value.status_code == 504
        assert "ran out of budget" in caught.value.detail

    @pytest.mark.asyncio
    async def test_an_aborted_fetch_becomes_a_gateway_timeout(self) -> None:
        """The script reports its own abort in the payload rather than throwing."""
        engine = sidecar.BrowserBackedSearch()

        class AbortingPage:
            async def evaluate(self, _script: str, _args: list) -> dict:
                return {"status": 0, "body": "", "error": "TimeoutError"}

        with pytest.raises(sidecar.HTTPException) as caught:
            await engine._evaluate(
                AbortingPage(),
                "script",
                [],
                what="Home Depot search",
                deadline=time.monotonic() + 5,
            )

        assert caught.value.status_code == 504
        assert "TimeoutError" in caught.value.detail

    @pytest.mark.asyncio
    async def test_the_remaining_budget_is_handed_to_the_script(self) -> None:
        """The script cannot abort itself without being told how long it has."""
        engine = sidecar.BrowserBackedSearch()
        seen: list[list] = []

        class RecordingPage:
            async def evaluate(self, _script: str, args: list) -> dict:
                seen.append(args)
                return {"status": 200, "body": "{}"}

        await engine._evaluate(
            RecordingPage(),
            "script",
            ["first", "second"],
            what="Home Depot search",
            deadline=time.monotonic() + 4,
        )

        assert seen[0][:2] == ["first", "second"]
        timeout_ms = seen[0][-1]
        assert isinstance(timeout_ms, int)
        assert 3_000 < timeout_ms <= 4_000, "the script gets what is left, in milliseconds"

    @pytest.mark.asyncio
    async def test_the_budget_stays_under_the_clients_own_timeout(self) -> None:
        """The sidecar has to fail first, or its caller is gone before it answers.

        The client waits 35s (`_DEFAULT_TIMEOUT_SECONDS` in sidecar_client.py).
        Budget plus grace is the worst case one request can take, and it has to
        land below that with room to spare.
        """
        from backend.app.integrations.supplier_pricing.sidecar_client import (
            _DEFAULT_TIMEOUT_SECONDS,
        )

        worst_case = sidecar.REQUEST_BUDGET_SECONDS + sidecar._EVALUATE_GRACE_SECONDS
        assert worst_case < _DEFAULT_TIMEOUT_SECONDS


class TestRequestTiming:
    @pytest.mark.asyncio
    async def test_queue_and_work_time_are_logged_separately(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A slow search and a queued one are different problems, and the logs
        could not tell them apart before (issue #1496)."""
        engine = sidecar.BrowserBackedSearch()

        with caplog.at_level("INFO", logger="hd-sidecar"):
            async with engine._timed_lock("home_depot", "home_depot search 'drill'"):
                pass

        assert any(
            "home_depot search 'drill'" in r.message
            and "queued" in r.message
            and "working" in r.message
            for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_timing_is_logged_even_when_the_request_fails(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A failed request is the one whose duration you most want to see."""
        engine = sidecar.BrowserBackedSearch()

        with caplog.at_level("INFO", logger="hd-sidecar"), pytest.raises(RuntimeError):
            async with engine._timed_lock("home_depot", "home_depot search 'drill'"):
                raise RuntimeError("boom")

        assert any("queued" in r.message for r in caplog.records)


class TestIdleBrowserRecycling:
    @pytest.mark.asyncio
    async def test_idle_browser_context_is_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        engine = sidecar.BrowserBackedSearch()
        closed: list[bool] = []

        class FakeContext:
            async def close(self) -> None:
                closed.append(True)

        monkeypatch.setattr(sidecar, "IDLE_SECONDS", 60)
        engine.state = "ready"
        engine._ctx = FakeContext()
        engine._page = object()
        engine._site_pages["home_depot"] = engine._page
        engine._site_pages["lowes"] = object()
        engine._lowes_failures = 1
        engine._last_used = time.monotonic() - 61

        assert await engine._evict_if_idle() is True
        assert engine.state == "idle"
        assert closed == [True]
        assert engine._ctx is None
        assert engine._page is None
        assert engine._site_pages == {}
        assert engine._lowes_failures == 0

    @pytest.mark.asyncio
    async def test_active_search_prevents_idle_eviction(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        engine = sidecar.BrowserBackedSearch()
        monkeypatch.setattr(sidecar, "IDLE_SECONDS", 60)
        engine.state = "ready"
        engine._active_requests = 1
        engine._last_used = time.monotonic() - 61

        assert await engine._evict_if_idle() is False
        assert engine.state == "ready"

    @pytest.mark.asyncio
    async def test_next_request_restarts_an_idle_browser(self) -> None:
        engine = sidecar.BrowserBackedSearch()
        started: list[bool] = []

        async def launch() -> None:
            started.append(True)
            engine.state = "ready"

        engine.state = "idle"
        engine._launch_browser = launch

        async with engine._use_browser():
            assert engine._active_requests == 1

        assert started == [True]
        assert engine._active_requests == 0
