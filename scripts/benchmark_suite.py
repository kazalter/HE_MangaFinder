#!/usr/bin/env python3
"""Reproducible fixed-scenario (B0-B6) replay & performance benchmark suite.

Runs completely isolated from production `.env`, databases, caches, and networks.
Outputs raw measurements, medians, and min-max ranges for baseline and post-optimization
comparisons.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
API_ROOT = REPO_ROOT / "apps" / "api"
COLLECTOR_ROOT = REPO_ROOT / "apps" / "social_collector"


def _clear_prod_env() -> None:
    for key in list(os.environ):
        if key.startswith(("MANGAFINDER_", "SOCIAL_COLLECTOR_")):
            os.environ.pop(key, None)


def _summary(values: list[float]) -> dict[str, float]:
    return {
        "median": round(statistics.median(values), 3),
        "min": round(min(values), 3),
        "max": round(max(values), 3),
        "raw": [round(v, 3) for v in values],
    }


def _make_test_png(width: int = 800, height: int = 1100, color: tuple[int, int, int] = (40, 90, 160)) -> bytes:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (width, height), color)
    draw = ImageDraw.Draw(img)
    for step in range(0, min(width, height) // 2, 80):
        draw.rectangle([step, step, width - step - 1, height - step - 1], outline=(220, 220, 220), width=4)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_test_jpeg(width: int = 600, height: int = 800, seed: int = 1) -> bytes:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (width, height), ((seed * 37) % 255, (seed * 73) % 255, (seed * 113) % 255))
    draw = ImageDraw.Draw(img)
    draw.rectangle([40, 40, width - 40, height - 40], fill=(240, 240, 240), outline=(20, 20, 20), width=6)
    draw.line([0, 0, width, height], fill=(10, 10, 10), width=8)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def run_b0_collector_idle(collector_python: str) -> dict[str, Any]:
    """B0: Collector lifespan startup with GraphQL session present (no browser needed)."""
    import subprocess

    script = """
import asyncio, json, os, tempfile
from pathlib import Path

async def run():
    with tempfile.TemporaryDirectory(prefix="mf-b0-") as tmp:
        tmp_path = Path(tmp)
        storage = tmp_path / "storage-state.json"
        runtime = tmp_path / "runtime-state.json"
        storage.write_text(
            json.dumps({"cookies": [{"name": "auth_token", "value": "dummy-auth"}, {"name": "ct0", "value": "dummy-ct0"}]}),
            encoding="utf-8",
        )
        os.environ["SOCIAL_COLLECTOR_STORAGE_STATE"] = str(storage)
        os.environ["SOCIAL_COLLECTOR_RUNTIME_STATE"] = str(runtime)
        os.environ["SOCIAL_COLLECTOR_TOKEN"] = "bench-token"

        import app.main as collector_main

        launched = 0

        class FakeContext:
            async def new_page(self):
                raise AssertionError("No page expected in B0")
            async def storage_state(self, path: str):
                Path(path).write_text("{}", encoding="utf-8")
            async def close(self):
                return None

        class FakeBrowser:
            async def new_context(self, **kwargs):
                return FakeContext()
            async def close(self):
                return None

        class FakeChromium:
            async def launch(self, **kwargs):
                nonlocal launched
                launched += 1
                return FakeBrowser()

        class FakePlaywright:
            def __init__(self):
                self.chromium = FakeChromium()
            async def stop(self):
                return None

        class FakeAsyncPlaywright:
            async def start(self):
                return FakePlaywright()

        collector_main.STATE_SOURCE = storage
        collector_main.STATE_RUNTIME = runtime
        collector_main.graphql_collector.state_path = storage
        collector_main.async_playwright = lambda: FakeAsyncPlaywright()
        collector_main.collector = collector_main.BrowserCollector()
        collector_main.global_metrics().counters.clear()

        async with collector_main.app.router.lifespan_context(collector_main.app):
            body = collector_main.health(None)

        print(json.dumps({
            "health_status": body["status"],
            "graphql_session_present": body["graphql_session_present"],
            "browser_started": body["browser_started"],
            "chromium_launches": body["chromium_launches"],
            "mock_launch_calls": launched,
            "collector_rss_kib": body["metrics"]["memory"]["rss_kib"],
        }))

asyncio.run(run())
"""
    proc = subprocess.run(
        [collector_python, "-c", script],
        cwd=str(COLLECTOR_ROOT),
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(COLLECTOR_ROOT)},
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout.strip())


async def run_b1_b2_catalog_discovery(repeats: int) -> dict[str, Any]:
    """B1 (repeated no-change scan) & B2 (incremental update + multi-alias)."""
    import httpx
    from app.core.observability import capture_metrics
    from app.db.base import Base
    from app.db.models import Author, CatalogEvent, Work
    from app.db.session import configure_sqlite_engine
    from app.modules.catalog.service import DiscoveryService
    from app.providers.nhentai import NhentaiProvider
    from app.providers.registry import ProviderRegistry
    from app.providers.wnacg import WnacgProvider
    from sqlalchemy import create_engine, func, select
    from sqlalchemy.orm import sessionmaker

    cover_png = _make_test_png(300, 400)

    def build_fixtures(extra_gallery: bool = False, updated_title: bool = False):
        galleries = []
        for idx in range(1, 11):
            gid = 90000 + idx
            title = f"[AuthorA] Work {idx}" + (" (Rev2)" if updated_title and idx == 1 else "")
            galleries.append(
                {
                    "id": gid,
                    "media_id": str(5000 + idx),
                    "english_title": title,
                    "japanese_title": f"[AuthorA] 作品 {idx}",
                    "thumbnail": f"galleries/{5000 + idx}/thumb.png",
                    "num_pages": 20 + idx,
                    "tag_ids": [101],
                }
            )
        if extra_gallery:
            galleries.insert(
                0,
                {
                    "id": 90099,
                    "media_id": "5099",
                    "english_title": "[AuthorA] Brand New Work 99",
                    "japanese_title": "[AuthorA] 新作 99",
                    "thumbnail": "galleries/5099/thumb.png",
                    "num_pages": 24,
                    "tag_ids": [101],
                },
            )
        return galleries

    def make_transport(state: dict[str, Any]):
        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "nhentai.net/api/v2/tags/artist/" in url:
                slug = url.rsplit("/", 1)[-1].split("?")[0]
                if slug == "authora":
                    return httpx.Response(200, json={"id": 101, "type": "artist", "name": "authora"})
                if slug == "authorb":
                    return httpx.Response(200, json={"id": 102, "type": "artist", "name": "authorb"})
                if slug in {"aliasa1", "aliasa2"}:
                    return httpx.Response(200, json={"id": 101, "type": "artist", "name": slug})
                if slug == "aliasb1":
                    return httpx.Response(200, json={"id": 102, "type": "artist", "name": slug})
                return httpx.Response(404)
            if "nhentai.net/api/v2/galleries/tagged" in url:
                return httpx.Response(
                    200,
                    json={
                        "result": build_fixtures(
                            extra_gallery=state["extra"],
                            updated_title=state["updated"],
                        ),
                        "num_pages": 1,
                    },
                )
            if "nhentai.net/api/v2/galleries/" in url:
                gid = url.rstrip("/").split("/")[-1]
                title = f"[AuthorA] Work {int(gid) - 90000}" if int(gid) <= 90010 else "[AuthorA] Brand New Work 99"
                if state["updated"] and gid == "90001":
                    title += " (Rev2)"
                return httpx.Response(
                    200,
                    json={
                        "id": int(gid),
                        "media_id": str(5000 + (int(gid) % 100)),
                        "title": {"english": title, "japanese": title, "pretty": title},
                        "tags": [{"id": 101, "type": "artist", "name": "authora"}, {"id": 201, "type": "tag", "name": "sole female"}],
                        "images": {"pages": [{"t": "p"} for _ in range(20)], "cover": {"t": "p"}},
                        "num_pages": 20,
                        "upload_date": 1700000000,
                    },
                )
            if "nhentai.net/galleries/" in url or "wnacgimg.date" in url:
                return httpx.Response(200, content=cover_png, headers={"content-type": "image/png"})
            if "wnacg.com/search/" in url:
                items = "".join(
                    f'<li class="gallary_item"><div class="pic_box cate-1">'
                    f'<a href="/photos-index-aid-{7000 + i}.html"><img src="//img.wnacgimg.date/c{i}.png"></a></div>'
                    f'<div class="info"><div class="title"><a href="/photos-index-aid-{7000 + i}.html" title="[AuthorA] Work {i}">[AuthorA] Work {i}</a></div>'
                    f'<div class="info_col">20張，2025-01-0{min(i,9)}</div></div></li>'
                    for i in range(1, 6)
                )
                return httpx.Response(200, text=f"<html><body><ul>{items}</ul></body></html>")
            return httpx.Response(404, json={"error": "not found"})

        return httpx.MockTransport(handler)

    fake_now = [1000.0]

    def fake_clock() -> float:
        return fake_now[0]

    async def noop_sleep(seconds: float) -> None:
        fake_now[0] += max(0.0, seconds)

    with tempfile.TemporaryDirectory(dir="/dev/shm", prefix="mf-b1-") as tmp:
        db_path = Path(tmp) / "bench.db"
        engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
        configure_sqlite_engine(engine)
        Base.metadata.create_all(engine)
        SessionFactory = sessionmaker(bind=engine, expire_on_commit=False)

        with SessionFactory() as session:
            authors = [
                Author(name="AuthorA", aliases=["AliasA1", "AliasA2"]),
                Author(name="AuthorB", aliases=["AliasB1"]),
            ]
            session.add_all(authors)
            session.commit()
            author_ids = [a.id for a in authors]

        state = {"extra": False, "updated": False}
        transport = make_transport(state)

        async def run_scan(aid: int):
            client = httpx.AsyncClient(transport=transport)
            nh = NhentaiProvider("UA", client=client, sleep=noop_sleep, clock=fake_clock)
            wn = WnacgProvider("UA", ["https://www.wnacg.com"], client=client, sleep=noop_sleep)
            reg = ProviderRegistry([nh, wn])
            try:
                with SessionFactory() as session:
                    with capture_metrics("bench.discovery") as m:
                        res = await DiscoveryService(session, reg).discover_author(aid)
                    return res, m.snapshot()
            finally:
                await reg.close()

        # Cold scan
        cold_snaps = []
        for aid in author_ids:
            _, snap = await run_scan(aid)
            cold_snaps.append(snap)

        # B1: Repeated no-change polls
        b1_wall, b1_cpu = [], []
        b1_counters_sample = {}
        for _ in range(repeats):
            t_wall = 0.0
            t_cpu = 0.0
            agg_c = {}
            for aid in author_ids:
                _, snap = await run_scan(aid)
                t_wall += snap["wall_ms"]
                t_cpu += snap["cpu_ms"]
                for k, v in snap["counters"].items():
                    agg_c[k] = agg_c.get(k, 0) + v
            b1_wall.append(t_wall)
            b1_cpu.append(t_cpu)
            b1_counters_sample = agg_c

        # B2: Incremental update
        state["extra"] = True
        state["updated"] = True
        b2_res, b2_snap = await run_scan(author_ids[0])
        with SessionFactory() as session:
            works_count = session.scalar(select(func.count(Work.id)))
            events_count = session.scalar(select(func.count(CatalogEvent.id)))

        engine.dispose()

        return {
            "b1_cold_counters": cold_snaps[0]["counters"],
            "b1_warm_wall_ms": _summary(b1_wall),
            "b1_warm_cpu_ms": _summary(b1_cpu),
            "b1_warm_counters": b1_counters_sample,
            "b2_result": b2_res,
            "b2_counters": b2_snap["counters"],
            "b2_total_works": works_count,
            "b2_total_events": events_count,
        }


async def run_b3_social_sync(repeats: int, collector_python: str) -> dict[str, Any]:
    """B3: X timeline sync with pinned old post, duplicates, and pagination resume."""
    from app.core.config import Settings
    from app.db.base import Base
    from app.db.models import Author, SocialAccount, SocialPost
    from app.db.session import configure_sqlite_engine
    from app.modules.social.collector import CollectorBatch, XBrowserCollector
    from app.modules.social.service import SocialSyncService
    from sqlalchemy import create_engine, func, select
    from sqlalchemy.orm import sessionmaker

    # Build a deterministic multi-page timeline with pinned old tweet + duplicate across pages
    def make_tweet(tid: str, created_at: str, text: str) -> dict[str, Any]:
        return {
            "__typename": "Tweet",
            "rest_id": tid,
            "legacy": {
                "id_str": tid,
                "created_at": created_at,
                "full_text": text,
            },
        }

    page1_payload = {
        "data": {
            "user": {
                "result": {
                    "timeline": {
                        "timeline": {
                            "instructions": [
                                {
                                    "entries": [
                                        # Pinned old tweet
                                        {
                                            "content": {
                                                "itemContent": {
                                                    "tweet_results": {
                                                        "result": make_tweet(
                                                            "100", "Mon Jan 01 10:00:00 +0000 2024", "pinned old"
                                                        )
                                                    }
                                                }
                                            }
                                        },
                                        {
                                            "content": {
                                                "itemContent": {
                                                    "tweet_results": {
                                                        "result": make_tweet(
                                                            "205", "Fri Nov 15 10:00:00 +0000 2026", "new post 205"
                                                        )
                                                    }
                                                }
                                            }
                                        },
                                        {
                                            "content": {
                                                "itemContent": {
                                                    "tweet_results": {
                                                        "result": make_tweet(
                                                            "204", "Thu Nov 14 10:00:00 +0000 2026", "new post 204"
                                                        )
                                                    }
                                                }
                                            }
                                        },
                                        {"content": {"entryType": "TimelineTimelineCursor", "cursorType": "Bottom", "value": "cur1"}},
                                    ]
                                }
                            ]
                        }
                    }
                }
            }
        }
    }
    page2_payload = {
        "data": {
            "user": {
                "result": {
                    "timeline": {
                        "timeline": {
                            "instructions": [
                                {
                                    "entries": [
                                        # Cross-page duplicate of 204 + older 203 + watermark 200
                                        {
                                            "content": {
                                                "itemContent": {
                                                    "tweet_results": {
                                                        "result": make_tweet(
                                                            "204", "Thu Nov 14 10:00:00 +0000 2026", "new post 204"
                                                        )
                                                    }
                                                }
                                            }
                                        },
                                        {
                                            "content": {
                                                "itemContent": {
                                                    "tweet_results": {
                                                        "result": make_tweet(
                                                            "203", "Wed Nov 13 10:00:00 +0000 2026", "new post 203"
                                                        )
                                                    }
                                                }
                                            }
                                        },
                                        {
                                            "content": {
                                                "itemContent": {
                                                    "tweet_results": {
                                                        "result": make_tweet(
                                                            "200", "Tue Nov 12 10:00:00 +0000 2026", "watermark 200"
                                                        )
                                                    }
                                                }
                                            }
                                        },
                                    ]
                                }
                            ]
                        }
                    }
                }
            }
        }
    }

    # Test collector WebGraphQLCollector via isolated collector venv subprocess
    import subprocess

    collector_script = """
import json, tempfile, time
from pathlib import Path
import curl_cffi.requests as cr
from app.graphql import WebGraphQLCollector
from app.observability import capture_metrics

page1 = %s
page2 = %s
user_resp = {"data": {"user": {"result": {"__typename": "User", "rest_id": "42", "core": {"screen_name": "artistx", "name": "Artist X"}}}}}

class FakeResp:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.content = json.dumps(payload).encode()
    def json(self):
        return self._payload

def fake_get(url, **kwargs):
    if "UserByScreenName" in url:
        return FakeResp(user_resp)
    if "cur1" in url:
        return FakeResp(page2)
    return FakeResp(page1)

cr.get = fake_get
with tempfile.TemporaryDirectory() as tmp:
    state = Path(tmp) / "state.json"
    state.write_text(json.dumps({"cookies": [{"name": "auth_token", "value": "a"}, {"name": "ct0", "value": "b"}]}))
    col = WebGraphQLCollector(state, "", "UA")
    walls, cpus = [], []
    last_snap = None
    last_batch = None
    for _ in range(%d):
        with capture_metrics() as m:
            t0, c0 = time.monotonic(), time.process_time()
            # First batch interrupted at limit=2 -> returns cursor graphql:cur1; second resumes
            b1 = col.posts_batch("artistx", "200", 2)
            b2 = col.posts_batch("artistx", "200", 10, cursor=b1["next_cursor"].removeprefix("graphql:"))
            walls.append((time.monotonic() - t0) * 1000)
            cpus.append((time.process_time() - c0) * 1000)
            last_snap = m.snapshot()
            last_batch = {"b1_ids": [p["id"] for p in b1["posts"]], "b1_complete": b1["complete"], "b2_ids": [p["id"] for p in b2["posts"]], "b2_complete": b2["complete"]}
    print(json.dumps({"walls": walls, "cpus": cpus, "counters": last_snap["counters"], "batch": last_batch}))
""" % (
        json.dumps(page1_payload),
        json.dumps(page2_payload),
        repeats,
    )

    proc = subprocess.run(
        [collector_python, "-c", collector_script],
        cwd=str(COLLECTOR_ROOT),
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(COLLECTOR_ROOT)},
        capture_output=True,
        text=True,
        check=True,
    )
    collector_data = json.loads(proc.stdout.strip())

    # Also verify API SocialSyncService watermark & progress persistence with mocked XBrowserCollector
    with tempfile.TemporaryDirectory(dir="/dev/shm", prefix="mf-b3-") as tmp:
        db_path = Path(tmp) / "social.db"
        engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
        configure_sqlite_engine(engine)
        Base.metadata.create_all(engine)
        SessionFactory = sessionmaker(bind=engine, expire_on_commit=False)

        with SessionFactory() as session:
            author = Author(name="ArtistX")
            session.add(author)
            session.flush()
            acc = SocialAccount(
                author_id=author.id,
                platform="x",
                handle="artistx",
                status="confirmed",
                avatar_url="https://example.invalid/avatar.jpg",
                last_post_id="200",
            )
            session.add(acc)
            session.commit()
            acc_id = acc.id

        # Simulate interrupted then resumed sync via SocialSyncService
        now = datetime.now(UTC)
        b1_resp = CollectorBatch.model_validate(
            {
                "posts": [
                    {"id": "205", "text": "p205", "url": "https://x.com/artistx/status/205", "posted_at": now.isoformat()},
                    {"id": "204", "text": "p204", "url": "https://x.com/artistx/status/204", "posted_at": (now - timedelta(hours=1)).isoformat()},
                ],
                "complete": False,
                "next_cursor": "graphql:cur1",
            }
        )
        b2_resp = CollectorBatch.model_validate(
            {
                "posts": [
                    {"id": "203", "text": "p203", "url": "https://x.com/artistx/status/203", "posted_at": (now - timedelta(hours=2)).isoformat()},
                ],
                "complete": True,
                "next_cursor": None,
            }
        )

        orig_posts_batch = XBrowserCollector.posts_batch
        responses = [b1_resp, b2_resp]

        async def fake_posts_batch(self, *args, **kwargs):
            return responses.pop(0)

        XBrowserCollector.posts_batch = fake_posts_batch
        try:
            settings = Settings(database_url=f"sqlite:///{db_path}", social_media_dir=Path(tmp) / "media")
            with SessionFactory() as session:
                r1 = await SocialSyncService(session, settings).sync_account(acc_id)
            with SessionFactory() as session:
                r2 = await SocialSyncService(session, settings).sync_account(acc_id)
                post_count = session.scalar(select(func.count(SocialPost.id)))
                watermark = session.get(SocialAccount, acc_id).last_post_id
        finally:
            XBrowserCollector.posts_batch = orig_posts_batch
            engine.dispose()

    return {
        "collector_wall_ms": _summary(collector_data["walls"]),
        "collector_cpu_ms": _summary(collector_data["cpus"]),
        "collector_counters": collector_data["counters"],
        "collector_batch": collector_data["batch"],
        "api_sync_step1": r1,
        "api_sync_step2": r2,
        "api_saved_posts": post_count,
        "api_final_watermark": watermark,
    }


async def run_b4_cover_maintenance(repeats: int, num_works: int = 200) -> dict[str, Any]:
    """B4: Cover maintenance over historical works (cold / warm / force)."""
    from app.core.observability import capture_metrics
    from app.db.base import Base
    from app.db.models import Work, WorkFingerprint, WorkSource
    from app.db.session import configure_sqlite_engine
    from app.modules.catalog.cover_maintenance import CoverFingerprintRefreshService
    from app.providers.base import ProviderCapability
    from app.providers.registry import ProviderRegistry
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    cover_bytes = _make_test_jpeg(400, 600, seed=7)

    class StubProvider:
        name = "stub"
        display_name = "Stub"
        capabilities = frozenset({ProviderCapability.AUTHOR_DISCOVERY})

        async def fetch_cover(self, external_id: str, url: str):
            from app.providers.base import RemoteImage

            return RemoteImage(content=cover_bytes, content_type="image/jpeg")

        async def close(self):
            return None

    with tempfile.TemporaryDirectory(dir="/dev/shm", prefix="mf-b4-") as tmp:
        db_path = Path(tmp) / "covers.db"
        engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
        configure_sqlite_engine(engine)
        Base.metadata.create_all(engine)
        SessionFactory = sessionmaker(bind=engine, expire_on_commit=False)

        with SessionFactory() as session:
            for i in range(num_works):
                w = Work(title=f"Cover Work {i}", cover_url=f"https://img.example/{i}.jpg")
                w.fingerprint = WorkFingerprint(normalized_title=f"cover work {i}")
                w.sources.append(WorkSource(provider="stub", external_id=str(i), source_url=f"https://img.example/{i}"))
                session.add(w)
            session.commit()

        reg = ProviderRegistry([StubProvider()])
        try:
            # Cold run: refresh first batch of 75, then remaining until done
            cold_wall, cold_cpu = [], []
            with SessionFactory() as session:
                with capture_metrics("b4.cold") as m:
                    r_cold = await CoverFingerprintRefreshService(session, reg).run(force=False, batch_size=75)
                cold_snap = m.snapshot()

            # Complete remaining cold items so cache becomes warm
            with SessionFactory() as session:
                while True:
                    res = await CoverFingerprintRefreshService(session, reg).run(force=False, batch_size=75)
                    if res["remaining"] == 0:
                        break

            # Warm no-op runs (5 repeats)
            warm_wall, warm_cpu = [], []
            warm_snap = {}
            for _ in range(repeats):
                with SessionFactory() as session:
                    with capture_metrics("b4.warm") as m:
                        r_warm = await CoverFingerprintRefreshService(session, reg).run(force=False, batch_size=75)
                    snap = m.snapshot()
                    warm_wall.append(snap["wall_ms"])
                    warm_cpu.append(snap["cpu_ms"])
                    warm_snap = snap

            # Force runs (5 repeats)
            force_wall, force_cpu = [], []
            force_snap = {}
            for _ in range(repeats):
                with SessionFactory() as session:
                    with capture_metrics("b4.force") as m:
                        r_force = await CoverFingerprintRefreshService(session, reg).run(force=True, batch_size=75)
                    snap = m.snapshot()
                    force_wall.append(snap["wall_ms"])
                    force_cpu.append(snap["cpu_ms"])
                    force_snap = snap
        finally:
            await reg.close()
            engine.dispose()

        return {
            "num_works": num_works,
            "cold_first_batch": {"result": r_cold, "counters": cold_snap["counters"], "wall_ms": cold_snap["wall_ms"], "cpu_ms": cold_snap["cpu_ms"]},
            "warm_noop": {"result": r_warm, "counters": warm_snap["counters"], "wall_ms": _summary(warm_wall), "cpu_ms": _summary(warm_cpu)},
            "force_run": {"result": r_force, "counters": force_snap["counters"], "wall_ms": _summary(force_wall), "cpu_ms": _summary(force_cpu), "peak_rss_kib": force_snap["memory"]["peak_rss_kib"]},
        }


async def run_b5_media_and_ocr(repeats: int) -> dict[str, Any]:
    """B5: Media archive + OCR with mixed valid/corrupt/oversized images."""
    import httpx
    from app.core.config import Settings
    from app.core.observability import capture_metrics
    from app.db.base import Base
    from app.db.models import Author, SocialAccount, SocialPost
    from app.db.session import configure_sqlite_engine
    from app.modules.social.media_archive import SocialMediaArchive
    from app.modules.social.ocr import LocalMediaOcr
    from PIL import Image
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    valid_jpg = _make_test_jpeg(1800, 2400, seed=3)
    # Create an EXIF-rotated JPEG
    exif_img = Image.new("RGB", (1200, 800), (200, 120, 40))
    exif = exif_img.getexif()
    exif[0x0112] = 6  # Rotate 90 CW
    buf_exif = BytesIO()
    exif_img.save(buf_exif, format="JPEG", exif=exif)
    rotated_jpg = buf_exif.getvalue()
    # Transparent PNG
    rgba_img = Image.new("RGBA", (900, 900), (10, 200, 100, 128))
    buf_rgba = BytesIO()
    rgba_img.save(buf_rgba, format="PNG")
    trans_png = buf_rgba.getvalue()
    corrupt_bytes = b"not-an-image-corrupt-header"

    payloads = {
        "https://pbs.twimg.com/media/valid.jpg": valid_jpg,
        "https://pbs.twimg.com/media/rotated.jpg": rotated_jpg,
        "https://pbs.twimg.com/media/trans.png": trans_png,
        "https://pbs.twimg.com/media/corrupt.jpg": corrupt_bytes,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url in payloads:
            return httpx.Response(200, content=payloads[url])
        return httpx.Response(404)

    wall_runs, cpu_runs = [], []
    last_snap = {}
    last_widths = []

    for _ in range(repeats):
        with tempfile.TemporaryDirectory(dir="/dev/shm", prefix="mf-b5-") as tmp:
            tmp_path = Path(tmp)
            db_path = tmp_path / "media.db"
            engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
            configure_sqlite_engine(engine)
            Base.metadata.create_all(engine)
            SessionFactory = sessionmaker(bind=engine, expire_on_commit=False)

            with SessionFactory() as session:
                author = Author(name="ArtistM")
                session.add(author)
                session.flush()
                acc = SocialAccount(author_id=author.id, platform="x", handle="artistm", status="confirmed")
                session.add(acc)
                session.flush()
                post = SocialPost(
                    account_id=acc.id,
                    platform_post_id="999",
                    url="https://x.com/artistm/status/999",
                    content_hash="hash-999",
                    posted_at=datetime.now(UTC),
                    media=[
                        {"type": "image", "url": "https://pbs.twimg.com/media/valid.jpg"},
                        {"type": "image", "url": "https://pbs.twimg.com/media/rotated.jpg"},
                        {"type": "image", "url": "https://pbs.twimg.com/media/trans.png"},
                        {"type": "image", "url": "https://pbs.twimg.com/media/corrupt.jpg"},
                    ],
                )
                session.add(post)
                session.commit()
                post_id = post.id

            settings = Settings(database_url=f"sqlite:///{db_path}", social_media_dir=tmp_path / "social-media")
            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            try:
                with SessionFactory() as session:
                    p = session.get(SocialPost, post_id)
                    with capture_metrics("b5.media") as m:
                        archived = await SocialMediaArchive(session, settings, client=client).archive_post(p)
                        # Check OCR path with stubbed subprocess if tesseract absent or test real path on local_paths
                        ocr = LocalMediaOcr(settings)
                        # Test OCR graceful handling
                        ocr_res = await ocr.extract_result(p.media, local_paths=archived)
                        session.commit()
                    last_snap = m.snapshot()
                    wall_runs.append(last_snap["wall_ms"])
                    cpu_runs.append(last_snap["cpu_ms"])
                    last_widths = sorted(
                        Image.open(path).size for path in archived.values()
                    )
            finally:
                await client.aclose()
                engine.dispose()

    return {
        "image_sizes_bytes": {k.rsplit("/", 1)[-1]: len(v) for k, v in payloads.items()},
        "archived_output_dimensions": last_widths,
        "ocr_status": ocr_res.status,
        "wall_ms": _summary(wall_runs),
        "cpu_ms": _summary(cpu_runs),
        "counters": last_snap["counters"],
        "peak_rss_kib": last_snap["memory"]["peak_rss_kib"],
    }


async def run_b6_fault_injection() -> dict[str, Any]:
    """B6: 429 Retry-After, transient SQLite lock, and job recovery."""
    import sqlite3

    import httpx
    from app.core.observability import capture_metrics
    from app.db.base import Base
    from app.db.models import Job, JobStatus, Work, WorkFingerprint
    from app.db.session import configure_sqlite_engine
    from app.modules.catalog import cover_maintenance
    from app.modules.catalog.cover_maintenance import CoverFingerprintRefreshService
    from app.modules.jobs.repository import DISCOVER_AUTHOR, JobRepository
    from app.providers.nhentai import NhentaiProvider
    from app.providers.registry import ProviderRegistry
    from sqlalchemy import create_engine
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import sessionmaker

    sleep_delays: list[float] = []
    fake_now = 100.0

    async def fake_sleep(seconds: float) -> None:
        nonlocal fake_now
        sleep_delays.append(seconds)
        fake_now += max(seconds, 0.0)

    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        if "tags/artist/" in str(request.url):
            return httpx.Response(200, json={"id": 1, "type": "artist", "name": "test"})
        if "galleries/tagged" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "result": [
                        {"id": 10, "media_id": "10", "english_title": "[Test] G1", "japanese_title": "", "num_pages": 12, "tag_ids": [1]}
                    ],
                    "num_pages": 1,
                },
            )
        return httpx.Response(
            200,
            json={
                "id": 10,
                "media_id": "10",
                "title": {"english": "[Test] G1", "pretty": "[Test] G1"},
                "tags": [{"id": 1, "type": "artist", "name": "test"}],
                "images": {"pages": [{"t": "j"}], "cover": {"t": "j"}},
                "num_pages": 1,
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    nh = NhentaiProvider("UA", client=client, sleep=fake_sleep, clock=lambda: fake_now)
    with capture_metrics("b6.rate_limit") as m_rl:
        works = await nh.discover_by_author("test")
    await client.aclose()

    # Transient SQLite lock + Job recovery
    with tempfile.TemporaryDirectory(dir="/dev/shm", prefix="mf-b6-") as tmp:
        db_path = Path(tmp) / "fault.db"
        engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
        configure_sqlite_engine(engine)
        Base.metadata.create_all(engine)
        SessionFactory = sessionmaker(bind=engine, expire_on_commit=False)

        with SessionFactory() as session:
            w = Work(title="lock-test", cover_url="https://img.example/1.jpg")
            w.fingerprint = WorkFingerprint(normalized_title="lock-test")
            session.add(w)
            j = Job(kind=DISCOVER_AUTHOR, payload={"author_id": 1}, status=JobStatus.RUNNING)
            session.add(j)
            session.commit()
            job_id = j.id

        with SessionFactory() as session:
            recovered = JobRepository(session).recover_interrupted()
            assert session.get(Job, job_id).status == JobStatus.PENDING

        orig_fp = cover_maintenance.CoverHasher.fingerprint_url

        async def stub_fp(self, url, provider, external_id):
            return {"version": 2, "variants": [{"dhash": "1234abcd"}]}

        cover_maintenance.CoverHasher.fingerprint_url = stub_fp
        try:
            with SessionFactory() as session:
                orig_commit = session.commit
                attempts = 0

                def flaky_commit():
                    nonlocal attempts
                    attempts += 1
                    if attempts == 1:
                        raise OperationalError("COMMIT", {}, sqlite3.OperationalError("database is locked"))
                    return orig_commit()

                session.commit = flaky_commit
                with capture_metrics("b6.sqlite_lock") as m_lock:
                    lock_res = await CoverFingerprintRefreshService(session, ProviderRegistry([])).run()
        finally:
            cover_maintenance.CoverHasher.fingerprint_url = orig_fp
            engine.dispose()

    return {
        "rate_limit_works": len(works),
        "rate_limit_counters": m_rl.snapshot()["counters"],
        "rate_limit_sleep_delays": sleep_delays,
        "jobs_recovered": recovered,
        "sqlite_lock_counters": m_lock.snapshot()["counters"],
        "sqlite_lock_refreshed": lock_res["refreshed"],
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--collector-python",
        type=str,
        default="/home/user1/.cache/mangafinder-venvs/venv-collector/bin/python",
    )
    args = parser.parse_args()

    _clear_prod_env()
    if str(API_ROOT) not in sys.path:
        sys.path.insert(0, str(API_ROOT))

    results: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "repeats": args.repeats,
        "b0": run_b0_collector_idle(args.collector_python),
        "b1_b2": await run_b1_b2_catalog_discovery(args.repeats),
        "b3": await run_b3_social_sync(args.repeats, args.collector_python),
        "b4": await run_b4_cover_maintenance(args.repeats),
        "b5": await run_b5_media_and_ocr(args.repeats),
        "b6": await run_b6_fault_injection(),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote benchmark results to {args.output}")


if __name__ == "__main__":
    asyncio.run(main())
