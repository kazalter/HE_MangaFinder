from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import main
from app.graphql import GraphQLTransientError, GraphQLUnavailable
from app.observability import capture_metrics


class _FakeLocator:
    def __init__(self, text: str = "timeline") -> None:
        self._text = text

    async def inner_text(self) -> str:
        return self._text

    async def count(self) -> int:
        return 0


class _FakeMouse:
    async def wheel(self, *_: object) -> None:
        return None


class _FakePage:
    def __init__(self, context: _FakeContext, *, url: str = "https://x.com/home") -> None:
        self.context = context
        self.url = url
        self.closed = False
        self.mouse = _FakeMouse()

    async def goto(self, url: str, **_: object) -> None:
        self.url = url

    async def wait_for_timeout(self, _: int) -> None:
        return None

    def locator(self, _: str) -> _FakeLocator:
        return _FakeLocator()

    async def close(self) -> None:
        self.closed = True
        self.context.pages_closed += 1


class _FakeContext:
    def __init__(self, *, tag: int, storage_payload: dict[str, object] | None = None) -> None:
        self.tag = tag
        self.storage_payload = storage_payload or {"cookies": [{"name": "tag", "value": str(tag)}]}
        self.closed = False
        self.pages_opened = 0
        self.pages_closed = 0
        self.saved_paths: list[str] = []

    async def new_page(self) -> _FakePage:
        self.pages_opened += 1
        return _FakePage(self)

    async def storage_state(self, path: str) -> None:
        self.saved_paths.append(path)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.storage_payload, fh)

    async def close(self) -> None:
        self.closed = True


class _FakeBrowser:
    def __init__(self, tracker: dict[str, object]) -> None:
        self.tracker = tracker
        self.closed = False

    async def new_context(self, **_: object) -> _FakeContext:
        self.tracker["new_context_calls"] += 1
        if self.tracker.get("fail_context"):
            raise RuntimeError("context failed")
        tag = int(self.tracker["new_context_calls"])
        ctx = _FakeContext(tag=tag)
        self.tracker["contexts"].append(ctx)
        return ctx

    async def close(self) -> None:
        self.closed = True
        self.tracker["browser_closes"] += 1


class _FakeChromium:
    def __init__(self, tracker: dict[str, object]) -> None:
        self.tracker = tracker

    async def launch(self, **_: object) -> _FakeBrowser:
        delay = float(self.tracker.get("launch_delay", 0.0))
        if delay:
            await asyncio.sleep(delay)
        self.tracker["launch_calls"] += 1
        if self.tracker.get("fail_launch"):
            raise RuntimeError("launch failed")
        browser = _FakeBrowser(self.tracker)
        self.tracker["browsers"].append(browser)
        return browser


class _FakePlaywright:
    def __init__(self, tracker: dict[str, object]) -> None:
        self.tracker = tracker
        self.chromium = _FakeChromium(tracker)
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True
        self.tracker["playwright_stops"] += 1


def _make_playwright_factory(tracker: dict[str, object]):
    class _Factory:
        async def start(self) -> _FakePlaywright:
            tracker["playwright_starts"] += 1
            pw = _FakePlaywright(tracker)
            tracker["playwrights"].append(pw)
            return pw

    return lambda: _Factory()


def _new_tracker(**overrides: object) -> dict[str, object]:
    tracker: dict[str, object] = {
        "playwright_starts": 0,
        "playwright_stops": 0,
        "launch_calls": 0,
        "new_context_calls": 0,
        "browser_closes": 0,
        "playwrights": [],
        "browsers": [],
        "contexts": [],
    }
    tracker.update(overrides)
    return tracker


def test_lifespan_health_and_graphql_only_skip_chromium(monkeypatch, tmp_path) -> None:
    state_source = tmp_path / "storage-state.json"
    state_runtime = tmp_path / "runtime-state.json"
    state_source.write_text(
        json.dumps(
            {"cookies": [{"name": "auth_token", "value": "a"}, {"name": "ct0", "value": "c"}]}
        ),
        encoding="utf-8",
    )
    state_runtime.write_text(json.dumps({"cookies": []}), encoding="utf-8")

    tracker = _new_tracker()
    fresh_collector = main.BrowserCollector()
    monkeypatch.setattr(main, "STATE_SOURCE", state_source)
    monkeypatch.setattr(main, "STATE_RUNTIME", state_runtime)
    monkeypatch.setattr(main, "collector", fresh_collector)
    monkeypatch.setattr(main, "async_playwright", _make_playwright_factory(tracker))
    invalidations = 0

    def _invalidate() -> None:
        nonlocal invalidations
        invalidations += 1

    monkeypatch.setattr(
        main,
        "graphql_collector",
        SimpleNamespace(
            available=True,
            lookup_user=lambda handle: {"handle": handle, "display_name": "X"},
            posts_batch=lambda *args, **kwargs: {
                "posts": [{"id": "10"}],
                "complete": True,
                "next_cursor": None,
            },
            invalidate_session=_invalidate,
        ),
    )

    async def scenario() -> dict[str, object]:
        with capture_metrics() as m:
            async with main.app.router.lifespan_context(main.app):
                for _ in range(2):
                    health = main.health(None)
                    assert health["status"] == "ok"
                    assert health["session_present"] is True
                    assert health["graphql_session_present"] is True
                    assert health["session_valid"] is None
                    assert health["browser_started"] is False
                    assert health["chromium_launches"] == 0

                check = await main.check_session()
                assert check == {"valid": True, "provider": "x_web_graphql"}

                batch = await main.posts_batch("artist")
                assert batch["posts"] == [{"id": "10"}]

                reloaded = await main.reload_session()
                assert reloaded == {"reloaded": True}
                assert not state_runtime.exists()
            return m.snapshot()

    snap = asyncio.run(scenario())
    assert invalidations == 1
    assert tracker["launch_calls"] == 0
    assert tracker["playwright_starts"] == 0
    assert snap["counters"].get("chromium_launches", 0) == 0


def test_session_check_preserves_graphql_transient_vs_unavailable_fallback(
    monkeypatch, tmp_path
) -> None:
    state_source = tmp_path / "storage-state.json"
    state_runtime = tmp_path / "runtime-state.json"
    state_source.write_text(json.dumps({"cookies": []}), encoding="utf-8")

    tracker = _new_tracker()
    fresh_collector = main.BrowserCollector()
    monkeypatch.setattr(main, "STATE_SOURCE", state_source)
    monkeypatch.setattr(main, "STATE_RUNTIME", state_runtime)
    monkeypatch.setattr(main, "collector", fresh_collector)
    monkeypatch.setattr(main, "async_playwright", _make_playwright_factory(tracker))

    async def scenario() -> None:
        async with main.app.router.lifespan_context(main.app):
            # 1. Transient error -> 503 without starting Chromium
            monkeypatch.setattr(
                main,
                "graphql_collector",
                SimpleNamespace(
                    available=True,
                    lookup_user=lambda _: (_ for _ in ()).throw(
                        GraphQLTransientError("proxy down")
                    ),
                ),
            )
            with pytest.raises(HTTPException) as exc_info:
                await main.check_session()
            assert exc_info.value.status_code == 503
            assert tracker["launch_calls"] == 0

            # 2. GraphQLUnavailable -> falls back to lazy browser check
            monkeypatch.setattr(
                main,
                "graphql_collector",
                SimpleNamespace(
                    available=True,
                    lookup_user=lambda _: (_ for _ in ()).throw(GraphQLUnavailable("expired")),
                ),
            )
            resp_ok = await main.check_session()
            assert resp_ok["provider"] == "browser"
            assert resp_ok["valid"] is True
            assert tracker["launch_calls"] == 1

    asyncio.run(scenario())


def test_concurrent_first_browser_requests_launch_single_chromium(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(main, "STATE_SOURCE", tmp_path / "missing-src.json")
    monkeypatch.setattr(main, "STATE_RUNTIME", tmp_path / "runtime.json")
    tracker = _new_tracker(launch_delay=0.02)
    monkeypatch.setattr(main, "async_playwright", _make_playwright_factory(tracker))

    async def scenario() -> None:
        bc = main.BrowserCollector()
        with capture_metrics() as m:
            results = await asyncio.gather(
                bc.check_session(),
                bc.check_session(),
                bc.check_session(),
            )
        assert results == [True, True, True]
        assert tracker["launch_calls"] == 1
        assert tracker["new_context_calls"] == 1
        ctx = tracker["contexts"][0]
        assert ctx.pages_opened == 3
        assert ctx.pages_closed == 3
        assert m.snapshot()["counters"].get("chromium_launches", 0) == 1

        # Close twice -> idempotent
        await bc.close()
        await bc.close()
        assert bc.browser is None
        assert bc.context is None
        assert bc.playwright is None
        assert tracker["browser_closes"] == 1
        assert tracker["playwright_stops"] == 1

    asyncio.run(scenario())


def test_startup_failure_cleans_up_and_allows_retry(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(main, "STATE_SOURCE", tmp_path / "missing-src.json")
    monkeypatch.setattr(main, "STATE_RUNTIME", tmp_path / "runtime.json")
    tracker = _new_tracker(fail_context=True)
    monkeypatch.setattr(main, "async_playwright", _make_playwright_factory(tracker))

    async def scenario() -> None:
        bc = main.BrowserCollector()
        with pytest.raises(RuntimeError, match="context failed"):
            await bc.check_session()

        assert bc.playwright is None
        assert bc.browser is None
        assert bc.context is None
        assert tracker["browser_closes"] == 1
        assert tracker["playwright_stops"] == 1

        tracker["fail_context"] = False
        assert await bc.check_session() is True
        assert tracker["launch_calls"] == 2
        assert tracker["new_context_calls"] == 2
        await bc.close()

    asyncio.run(scenario())


def test_reload_waits_for_active_page_and_cancellation_does_not_write_stale_state(
    monkeypatch, tmp_path
) -> None:
    state_source = tmp_path / "storage-state.json"
    state_runtime = tmp_path / "runtime-state.json"
    state_source.write_text(
        json.dumps({"cookies": [{"name": "fresh", "value": "2"}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(main, "STATE_SOURCE", state_source)
    monkeypatch.setattr(main, "STATE_RUNTIME", state_runtime)
    tracker = _new_tracker()
    monkeypatch.setattr(main, "async_playwright", _make_playwright_factory(tracker))

    async def scenario() -> None:
        bc = main.BrowserCollector()
        nav_started = asyncio.Event()
        release_nav = asyncio.Event()

        async def slow_navigate(page: _FakePage, url: str) -> None:
            page.url = url
            nav_started.set()
            await release_nav.wait()

        monkeypatch.setattr(bc, "navigate", slow_navigate)

        # 1. Cancellation during active posts_batch must not write runtime-state.json
        cancelled_task = asyncio.create_task(bc.posts_batch("artist", None, 10))
        await nav_started.wait()
        cancelled_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled_task
        assert not state_runtime.exists()
        first_ctx = tracker["contexts"][0]
        assert first_ctx.pages_closed == 1

        # 2. Reload during active scan waits for page exit and replaces context cleanly
        nav_started.clear()
        release_nav.clear()

        async def normal_after_release(page: _FakePage, url: str) -> None:
            page.url = url
            nav_started.set()
            await release_nav.wait()

        async def fake_ensure_access(_page: _FakePage) -> None:
            return None

        monkeypatch.setattr(bc, "navigate", normal_after_release)
        monkeypatch.setattr(bc, "ensure_access", fake_ensure_access)

        batch_task = asyncio.create_task(bc.posts_batch("artist", None, 10))
        await nav_started.wait()
        reload_task = asyncio.create_task(bc.reload_session())
        await asyncio.sleep(0.01)
        assert not reload_task.done()

        release_nav.set()
        batch_result = await batch_task
        assert batch_result["complete"] is True
        await reload_task

        assert first_ctx.closed is True
        assert not state_runtime.exists()
        assert len(tracker["contexts"]) == 2
        assert bc.context is tracker["contexts"][1]
        await bc.close()

    asyncio.run(scenario())
