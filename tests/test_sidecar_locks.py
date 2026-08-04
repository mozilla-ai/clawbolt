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
            if call.func.attr != "_lock_for" or not call.args:
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
