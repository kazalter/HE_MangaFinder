from __future__ import annotations

import asyncio
import json
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import graphql, main
from app.observability import capture_metrics


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | str) -> None:
        self.status_code = status_code
        self._payload = payload
        if isinstance(payload, str):
            self.text = payload
            self.content = payload.encode()
        else:
            self.text = json.dumps(payload)
            self.content = self.text.encode()

    def json(self) -> dict:
        if isinstance(self._payload, str):
            raise ValueError("not json")
        return self._payload


def _write_state(path, auth: str = "auth1", ct0: str = "ct0-1") -> None:
    path.write_text(
        json.dumps(
            {"cookies": [{"name": "auth_token", "value": auth}, {"name": "ct0", "value": ct0}]}
        ),
        encoding="utf-8",
    )


def _user_payload(handle: str = "artist", rest_id: str = "42") -> dict:
    return {
        "data": {
            "user": {
                "result": {
                    "__typename": "User",
                    "rest_id": rest_id,
                    "core": {"screen_name": handle, "name": "Artist"},
                    "legacy": {"description": "bio", "profile_image_url_https": "https://img/a.jpg"},
                }
            }
        }
    }


def _tweet_item(post_id: str, posted_at: str = "Mon Jul 13 12:24:32 +0000 2026") -> dict:
    return {
        "content": {
            "itemContent": {
                "tweet_results": {
                    "result": {
                        "__typename": "Tweet",
                        "rest_id": post_id,
                        "legacy": {
                            "created_at": posted_at,
                            "conversation_id_str": post_id,
                            "full_text": f"post {post_id}",
                        },
                        "core": {"user_results": {"result": {"core": {"screen_name": "artist"}}}},
                    }
                }
            }
        }
    }


def test_single_session_per_batch_and_thread_isolation(monkeypatch, tmp_path) -> None:
    state = tmp_path / "storage-state.json"
    _write_state(state)

    sessions_created: list[dict[str, object]] = []
    sessions_lock = threading.Lock()

    class _FakeCookies(dict):
        pass

    class _FakeSession:
        def __init__(self) -> None:
            self.thread_id = threading.get_ident()
            self.cookies = _FakeCookies({"stale": "1"})
            self.urls: list[str] = []
            self.closed = False
            self.record = {
                "thread_id": self.thread_id,
                "urls": self.urls,
                "closed": False,
                "cookies_cleared": 0,
            }
            orig_clear = self.cookies.clear

            def _tracked_clear() -> None:
                self.record["cookies_cleared"] += 1
                orig_clear()

            self.cookies.clear = _tracked_clear
            with sessions_lock:
                sessions_created.append(self.record)

        def get(self, url: str, **kwargs: object) -> _FakeResponse:
            assert threading.get_ident() == self.thread_id
            assert kwargs["timeout"] <= graphql.DEFAULT_REQUEST_TIMEOUT_SECONDS
            self.urls.append(url)
            if "UserByScreenName" in url:
                return _FakeResponse(200, _user_payload())
            if "cur1" in url:
                entries = [_tweet_item("102"), _tweet_item("101")]
            else:
                entries = [
                    _tweet_item("103"),
                    {
                        "content": {
                            "entryType": "TimelineTimelineCursor",
                            "cursorType": "Bottom",
                            "value": "cur1",
                        }
                    },
                ]
            return _FakeResponse(
                200,
                {
                    "data": {
                        "user": {
                            "result": {
                                "__typename": "User",
                                "timeline": {"timeline": {"instructions": [{"entries": entries}]}},
                            }
                        }
                    }
                },
            )

        def close(self) -> None:
            self.closed = True
            self.record["closed"] = True

    monkeypatch.setattr(graphql.requests, "Session", _FakeSession)
    collector = graphql.WebGraphQLCollector(state, "", "UA")

    with capture_metrics() as m:
        res = collector.posts_batch("artist", None, 10)

    assert [p["id"] for p in res["posts"]] == ["103", "102", "101"]
    assert res["complete"] is True
    assert len(sessions_created) == 1
    assert len(sessions_created[0]["urls"]) == 3  # 1 user lookup + 2 timeline pages
    assert sessions_created[0]["closed"] is True
    assert sessions_created[0]["cookies_cleared"] >= 1
    snap = m.snapshot()
    assert snap["counters"]["graphql_pool_opens"] == 1
    assert snap["counters"]["graphql_pool_closes"] == 1


def test_user_cache_ttl_mismatch_and_transient_error_not_cached(monkeypatch, tmp_path) -> None:
    state = tmp_path / "storage-state.json"
    _write_state(state)
    now = 1000.0
    calls = 0
    mode = "ok"

    collector = graphql.WebGraphQLCollector(state, "", "UA", clock=lambda: now)

    def fake_request(operation: str, variables: dict, *_args, **_kwargs) -> dict:
        nonlocal calls
        calls += 1
        if mode == "429":
            raise graphql.GraphQLTransientError("429")
        if mode == "missing":
            return {"data": {"user": {"result": {"__typename": "UserUnavailable"}}}}
        if mode == "renamed":
            return _user_payload(handle="new_name", rest_id="42")
        return _user_payload(handle=variables["screen_name"], rest_id="42")

    monkeypatch.setattr(collector, "_request", fake_request)

    # 1. Transient 429 must NOT populate negative cache
    mode = "429"
    with pytest.raises(graphql.GraphQLTransientError):
        collector.lookup_user("artist")
    mode = "ok"
    user = collector.lookup_user("@Artist")
    assert user is not None and user["id"] == "42"
    assert calls == 2

    # 2. Warm hit skips upstream
    with capture_metrics() as m:
        cached = collector.lookup_user("artist")
    assert cached == user
    assert calls == 2
    assert m.snapshot()["counters"]["identity_cache_hits"] == 1

    # 3. After TTL expires, refreshes from upstream
    now += graphql.USER_CACHE_TTL_SECONDS + 1.0
    collector.lookup_user("artist")
    assert calls == 3

    # 4. Renamed / mismatched handle is NOT cached under old handle
    mode = "renamed"
    renamed = collector.lookup_user("old_name")
    assert renamed is not None and renamed["handle"] == "new_name"
    mode = "ok"
    collector.lookup_user("old_name")
    assert calls == 5

    # 5. Confirmed missing uses short negative TTL
    mode = "missing"
    assert collector.lookup_user("nobody") is None
    assert collector.lookup_user("nobody") is None
    assert calls == 6
    now += graphql.NEGATIVE_USER_CACHE_TTL_SECONDS + 1.0
    mode = "ok"
    assert collector.lookup_user("nobody")["id"] == "42"
    assert calls == 7


def test_credential_generation_invalidation_and_snapshot_consistency(monkeypatch, tmp_path) -> None:
    state = tmp_path / "storage-state.json"
    _write_state(state, auth="auth-1", ct0="csrf-1")

    seen_headers: list[tuple[str, str]] = []

    def fake_get(url: str, *, headers: dict[str, str], **_: object) -> _FakeResponse:
        seen_headers.append((headers["Cookie"], headers["x-csrf-token"]))
        return _FakeResponse(200, _user_payload())

    monkeypatch.setattr(graphql.requests, "get", fake_get)
    collector = graphql.WebGraphQLCollector(state, "", "UA")
    assert collector.lookup_user("artist")["id"] == "42"

    # External file update changes size/mtime -> reloads snapshot and invalidates user cache
    _write_state(state, auth="auth-22", ct0="csrf-22")
    assert collector.lookup_user("artist")["id"] == "42"

    # Explicit reload/invalidate increments generation even if same file contents
    collector.invalidate_session()
    assert collector.lookup_user("artist")["id"] == "42"

    assert len(seen_headers) == 3
    assert "auth_token=auth-1" in seen_headers[0][0] and seen_headers[0][1] == "csrf-1"
    assert "auth_token=auth-22" in seen_headers[1][0] and seen_headers[1][1] == "csrf-22"
    assert "auth_token=auth-22" in seen_headers[2][0] and seen_headers[2][1] == "csrf-22"


def test_concurrent_query_id_refresh_coordinated(tmp_path) -> None:
    state = tmp_path / "storage-state.json"
    _write_state(state)
    collector = graphql.WebGraphQLCollector(state, "", "UA")

    discover_calls = 0
    discover_lock = threading.Lock()
    barrier = threading.Barrier(2)

    def fake_discover() -> None:
        nonlocal discover_calls
        time.sleep(0.03)
        with discover_lock:
            discover_calls += 1
            collector.query_ids["UserByScreenName"] = "freshQID"

    def fake_get(url: str, **_: object) -> _FakeResponse:
        if "freshQID" in url:
            return _FakeResponse(200, _user_payload())
        barrier.wait(timeout=2.0)
        return _FakeResponse(404, {})

    collector._discover_query_ids = fake_discover
    collector._get = fake_get

    results: list[dict | None] = []

    def worker(handle: str) -> None:
        results.append(collector.lookup_user(handle))

    t1 = threading.Thread(target=worker, args=("artist1",))
    t2 = threading.Thread(target=worker, args=("artist2",))
    t1.start()
    t2.start()
    t1.join(timeout=3.0)
    t2.join(timeout=3.0)

    assert discover_calls == 1
    assert len(results) == 2 and all(item is not None for item in results)


def test_budget_exhaustion_returns_progress_only_after_advanced_page(tmp_path) -> None:
    state = tmp_path / "storage-state.json"
    _write_state(state)
    now = 0.0
    collector = graphql.WebGraphQLCollector(state, "", "UA", clock=lambda: now)
    collector.lookup_user = lambda handle: {"id": "42", "handle": handle}

    def fake_request(operation: str, variables: dict, *_args) -> dict:
        nonlocal now
        cursor = variables.get("cursor")
        now += 11.0  # Exceeds 10.0s budget before page 2
        if cursor is None:
            entries = [
                _tweet_item("300"),
                {
                    "content": {
                        "entryType": "TimelineTimelineCursor",
                        "cursorType": "Bottom",
                        "value": "cur1",
                    }
                },
            ]
        else:
            entries = [_tweet_item("200")]
        return {
            "data": {
                "user": {
                    "result": {
                        "timeline": {"timeline": {"instructions": [{"entries": entries}]}}
                    }
                }
            }
        }

    collector._request = fake_request

    # 1. Budget too tight for page 1 -> fail closed
    now = 0.0
    collector._request = lambda *args: (
        (_ for _ in ()).throw(AssertionError("should not be reached"))
    )
    with pytest.raises(graphql.GraphQLTransientError, match="时间预算"):
        collector.posts_batch("artist", None, 10, budget_seconds=0.01)

    # 2. Budget expires after page 1 completes -> returns durable cursor graphql:cur1
    now = 0.0
    collector._request = fake_request
    batch1 = collector.posts_batch("artist", None, 10, budget_seconds=10.0)
    assert [p["id"] for p in batch1["posts"]] == ["300"]
    assert batch1["complete"] is False
    assert batch1["next_cursor"] == "graphql:cur1"

    # 3. Resume from graphql:cur1 completes cleanly with full combined post set
    now = 0.0
    batch2 = collector.posts_batch(
        "artist",
        None,
        10,
        cursor=batch1["next_cursor"].removeprefix("graphql:"),
        budget_seconds=10.0,
    )
    assert [p["id"] for p in batch2["posts"]] == ["200"]
    assert batch2["complete"] is True
    assert batch2["next_cursor"] is None


def test_cancelled_graphql_holds_slot_until_worker_exits(monkeypatch, tmp_path) -> None:
    state = tmp_path / "storage-state.json"
    _write_state(state)

    started = threading.Event()
    allow_exit = threading.Event()
    saw_cancel = False

    def blocking_posts_batch(
        handle: str,
        since_id: str | None,
        limit: int,
        cursor: str | None = None,
        stop_before: str | None = None,
        *,
        budget_seconds: float | None = None,
        cancel_event: threading.Event | None = None,
    ) -> dict[str, object]:
        nonlocal saw_cancel
        started.set()
        assert cancel_event is not None
        assert cancel_event.wait(timeout=2.0)
        saw_cancel = True
        allow_exit.wait(timeout=2.0)
        raise graphql.GraphQLTransientError("cancelled")

    monkeypatch.setattr(main, "GRAPHQL_MAX_WORKERS", 1)
    monkeypatch.setattr(main, "GRAPHQL_SLOT_WAIT_SECONDS", 0.05)
    monkeypatch.setattr(
        main,
        "graphql_collector",
        SimpleNamespace(available=True, posts_batch=blocking_posts_batch),
    )

    async def scenario() -> None:
        async with main.app.router.lifespan_context(main.app):
            task = asyncio.create_task(main.posts_batch("artist"))
            while not started.is_set():
                await asyncio.sleep(0.01)

            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            # While worker thread is still winding down, slot MUST still be occupied (502/503)
            with pytest.raises(HTTPException) as exc_info:
                await main.posts_batch("artist")
            assert exc_info.value.status_code == 502

            # Once worker actually exits, slot releases and subsequent calls proceed cleanly
            allow_exit.set()
            await asyncio.sleep(0.05)

            monkeypatch.setattr(
                main.graphql_collector,
                "posts_batch",
                lambda *args, **kwargs: {
                    "posts": [{"id": "1"}],
                    "complete": True,
                    "next_cursor": None,
                },
            )
            res = await main.posts_batch("artist")
            assert res["posts"] == [{"id": "1"}]

    asyncio.run(scenario())
    assert saw_cancel is True
