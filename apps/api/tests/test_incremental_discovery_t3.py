from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.base import Base
from app.db.models import (
    Author,
    CatalogEvent,
    ProviderAuthorIdentity,
    ProviderScanRun,
    ProviderScanState,
    Work,
    WorkSource,
)
from app.modules.catalog.repository import CatalogRepository
from app.modules.catalog.service import DiscoveryService
from app.providers.base import DiscoveredWork
from app.providers.nhentai import NhentaiProvider
from app.providers.registry import ProviderRegistry


class FakeClock:
    def __init__(self) -> None:
        self.value = 1000.0

    def now(self) -> float:
        return self.value

    async def sleep(self, seconds: float) -> None:
        self.value += max(0.0, seconds)


def _make_memory_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine, expire_on_commit=False)


def _patch_settings(monkeypatch: pytest.MonkeyPatch, **overrides: object) -> Settings:
    settings = Settings(database_url="sqlite:///:memory:", **overrides)
    monkeypatch.setattr("app.modules.catalog.service.get_settings", lambda: settings)
    return settings


def test_partial_listing_preserves_existing_details_while_detail_clear_propagates() -> None:
    with _make_memory_session() as session:
        author = Author(name="AuthorA")
        session.add(author)
        session.flush()
        repo = CatalogRepository(session)

        repo.upsert_with_result(
            author.id,
            "nhentai",
            DiscoveredWork(
                external_id="1001",
                title="Original Title",
                source_url="https://nhentai.net/g/1001/",
                description="Rich synopsis",
                cover_url="https://t.nhentai.net/galleries/10/cover.jpg",
                status="completed",
                year=2025,
                language="ja",
                tags=["comedy", "romance"],
                source_updated_at=datetime(2025, 1, 5, tzinfo=UTC),
                raw_metadata={
                    "altTitles": ["原題"],
                    "page_count": 20,
                    "artists": ["authora", "coauthor"],
                    "media_id": "10",
                    "scanlator": "TeamAlpha",
                    "uploaded_at": "2025-01-05T00:00:00+00:00",
                },
                list_signature="sig-v1",
                is_detail_complete=True,
                detail_fetched=True,
            ),
        )
        session.commit()

        # Subsequent listing-only update changes title + page_count,
        # while omitting tags/description/language/artists.
        res = repo.upsert_with_result(
            author.id,
            "nhentai",
            DiscoveredWork(
                external_id="1001",
                title="Updated Title",
                source_url="https://nhentai.net/g/1001/",
                cover_url="https://t.nhentai.net/galleries/10/cover.jpg",
                status="completed",
                language="und",
                tags=[],
                raw_metadata={
                    "altTitles": ["原題", "Rev"],
                    "page_count": 24,
                    "artists": ["authora"],
                    "media_id": "10",
                },
                provided_fields=frozenset({"cover_url", "status", "raw_metadata"}),
                raw_metadata_provided_keys=frozenset({"altTitles", "page_count", "media_id"}),
                list_signature="sig-v2",
                is_detail_complete=False,
                detail_fetched=False,
            ),
        )
        session.commit()

        assert res.metadata_changed is True
        work = session.scalar(select(Work))
        source = session.scalar(select(WorkSource))
        assert work.title == "Updated Title"
        assert work.description == "Rich synopsis"
        assert work.language == "ja"
        assert work.year == 2025
        assert work.tags == ["comedy", "romance"]
        assert source.raw_metadata["artists"] == ["authora", "coauthor"]
        assert source.raw_metadata["scanlator"] == "TeamAlpha"
        assert source.raw_metadata["page_count"] == 24
        assert source.list_signature == "sig-v1"

        # Explicit detail payload clearing tags propagates.
        repo.upsert_with_result(
            author.id,
            "nhentai",
            DiscoveredWork(
                external_id="1001",
                title="Updated Title",
                source_url="https://nhentai.net/g/1001/",
                cover_url="https://t.nhentai.net/galleries/10/cover.jpg",
                status="completed",
                year=None,
                language="und",
                tags=[],
                raw_metadata={"artists": ["authora"], "media_id": "10", "page_count": 24},
                provided_fields=frozenset(
                    {
                        "cover_url",
                        "status",
                        "year",
                        "language",
                        "tags",
                        "source_updated_at",
                        "raw_metadata",
                    }
                ),
                raw_metadata_provided_keys=None,
                list_signature="sig-v2",
                is_detail_complete=True,
                detail_fetched=True,
            ),
        )
        session.commit()
        session.refresh(work)
        session.refresh(source)
        assert work.tags == []
        assert work.language == "und"
        assert source.list_signature == "sig-v2"


async def test_detail_backlog_covers_25_items_with_budget_8_and_zero_refetch_when_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_settings(
        monkeypatch,
        catalog_detail_max_fetches_per_run=8,
        catalog_detail_cache_ttl_hours=168,
    )
    detail_calls: list[str] = []
    clock = FakeClock()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/api/v2/tags/artist/authora" in url:
            return httpx.Response(200, json={"id": 101, "type": "artist", "name": "authora"})
        if "/api/v2/galleries/tagged" in url:
            items = [
                {
                    "id": 5000 + i,
                    "media_id": str(8000 + i),
                    "english_title": f"[AuthorA] Work {i}",
                    "num_pages": 10 + i,
                    "tag_ids": [101],
                }
                for i in range(1, 26)
            ]
            return httpx.Response(200, json={"result": items, "num_pages": 1})
        if "/api/v2/galleries/" in url:
            gid = url.rstrip("/").split("/")[-1]
            detail_calls.append(gid)
            return httpx.Response(
                200,
                json={
                    "id": int(gid),
                    "media_id": str(8000 + int(gid) - 5000),
                    "title": {"pretty": f"Work {int(gid) - 5000}"},
                    "tags": [
                        {"id": 101, "type": "artist", "name": "authora"},
                        {"id": 300, "type": "tag", "name": "full color"},
                    ],
                    "images": {"pages": [{"t": "j"}]},
                    "upload_date": 1700000000,
                },
            )
        return httpx.Response(404)

    with _make_memory_session() as session:
        author = Author(name="AuthorA")
        session.add(author)
        session.commit()

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = NhentaiProvider(
                "UA", client=client, sleep=clock.sleep, clock=clock.now
            )
            service = DiscoveryService(session, ProviderRegistry([provider]))

            for _ in range(4):
                await service.discover_author(author.id)
            assert len(detail_calls) == 25
            assert len(set(detail_calls)) == 25

            detail_calls.clear()
            await service.discover_author(author.id)
            assert detail_calls == []

            for src in session.scalars(select(WorkSource)):
                src.detail_fetched_at = datetime.now(UTC) - timedelta(hours=200)
            session.commit()
            for _ in range(4):
                await service.discover_author(author.id)
            assert len(set(detail_calls)) == 25


async def test_failed_detail_does_not_extend_ttl_and_retries_next_round(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_settings(monkeypatch, catalog_detail_max_fetches_per_run=8)
    fail_id_5002 = [True]
    detail_calls: list[str] = []
    clock = FakeClock()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/api/v2/tags/artist/authora" in url:
            return httpx.Response(200, json={"id": 101, "type": "artist", "name": "authora"})
        if "/api/v2/galleries/tagged" in url:
            return httpx.Response(
                200,
                json={
                    "result": [
                        {
                            "id": 5001,
                            "media_id": "1",
                            "english_title": "W1",
                            "num_pages": 5,
                            "tag_ids": [101],
                        },
                        {
                            "id": 5002,
                            "media_id": "2",
                            "english_title": "W2",
                            "num_pages": 6,
                            "tag_ids": [101],
                        },
                    ],
                    "num_pages": 1,
                },
            )
        if "/api/v2/galleries/" in url:
            gid = url.rstrip("/").split("/")[-1]
            detail_calls.append(gid)
            if gid == "5002" and fail_id_5002[0]:
                return httpx.Response(500)
            return httpx.Response(
                200,
                json={
                    "id": int(gid),
                    "media_id": gid[-1],
                    "title": {"pretty": f"Detail {gid}"},
                    "tags": [{"id": 101, "type": "artist", "name": "authora"}],
                    "images": {"pages": [{"t": "j"}]},
                    "upload_date": 1700000000,
                },
            )
        return httpx.Response(404)

    with _make_memory_session() as session:
        author = Author(name="AuthorA")
        session.add(author)
        session.commit()

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = NhentaiProvider(
                "UA", client=client, sleep=clock.sleep, clock=clock.now
            )
            service = DiscoveryService(session, ProviderRegistry([provider]))

            await service.discover_author(author.id)
            src_5001 = session.scalar(
                select(WorkSource).where(WorkSource.external_id == "5001")
            )
            src_5002 = session.scalar(
                select(WorkSource).where(WorkSource.external_id == "5002")
            )
            assert src_5001.detail_fetched_at is not None
            assert src_5002.detail_fetched_at is None
            assert src_5002.detail_failure_count == 1

            detail_calls.clear()
            fail_id_5002[0] = False
            await service.discover_author(author.id)
            assert detail_calls == ["5002"]
            session.refresh(src_5002)
            assert src_5002.detail_fetched_at is not None
            assert src_5002.detail_failure_count == 0


async def test_identity_cache_merges_same_tag_preserves_distinct_and_handles_invalidation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_settings(monkeypatch, catalog_identity_cache_ttl_hours=168)
    tag_lookups: list[str] = []
    tagged_scans: list[str] = []
    inject_404_tag_101 = [False]
    rate_limit_slug = [False]
    clock = FakeClock()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/api/v2/tags/artist/" in url:
            slug = url.rsplit("/", 1)[-1]
            if rate_limit_slug[0] and slug == "circleb":
                return httpx.Response(429, headers={"Retry-After": "1"})
            tag_lookups.append(slug)
            if slug in {"authora", "alias1"}:
                resolved = 109 if inject_404_tag_101[0] else 101
                return httpx.Response(200, json={"id": resolved, "type": "artist", "name": slug})
            if slug == "circleb":
                return httpx.Response(200, json={"id": 205, "type": "artist", "name": "circleb"})
            return httpx.Response(404)
        if "/api/v2/search" in url:
            return httpx.Response(200, json={"result": [], "num_pages": 1})
        if "/api/v2/galleries/tagged" in url:
            tid = request.url.params["tag_id"]
            if inject_404_tag_101[0] and tid == "101":
                return httpx.Response(404)
            tagged_scans.append(tid)
            gid = 1 if tid in {"101", "109"} else 2
            return httpx.Response(
                200,
                json={
                    "result": [
                        {
                            "id": gid,
                            "media_id": str(gid),
                            "english_title": f"Work {gid}",
                            "tag_ids": [int(tid)],
                        }
                    ],
                    "num_pages": 1,
                },
            )
        if "/api/v2/galleries/" in url:
            gid = url.rstrip("/").split("/")[-1]
            tag_obj = (
                {"id": 101, "type": "artist", "name": "authora"}
                if gid == "1"
                else {"id": 205, "type": "artist", "name": "circleb"}
            )
            return httpx.Response(
                200,
                json={
                    "id": int(gid),
                    "media_id": gid,
                    "title": {"pretty": f"Work {gid}"},
                    "tags": [tag_obj],
                    "images": {"pages": [{"t": "j"}]},
                },
            )
        return httpx.Response(404)

    with _make_memory_session() as session:
        author = Author(name="AuthorA", aliases=["Alias1", "CircleB"])
        session.add(author)
        session.commit()

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = NhentaiProvider(
                "UA", client=client, sleep=clock.sleep, clock=clock.now
            )
            service = DiscoveryService(session, ProviderRegistry([provider]))

            await service.discover_author(author.id)
            assert tagged_scans == ["101", "205"]
            assert len(list(session.scalars(select(ProviderScanState)))) == 2

            tag_lookups.clear()
            tagged_scans.clear()
            await service.discover_author(author.id)
            assert tag_lookups == []
            assert tagged_scans == ["101", "205"]

            author.aliases = ["Alias1", "CircleB", "NewAlias"]
            session.commit()
            await service.discover_author(author.id)
            assert len(tag_lookups) >= 1

            # Cached tag 404 (tag merged on source 101 -> 109) invalidates cache,
            # re-resolves live to 109, and completes cleanly!
            inject_404_tag_101[0] = True
            tag_lookups.clear()
            tagged_scans.clear()
            res_re = await service.discover_author(author.id)
            assert res_re["status"] == "succeeded"
            assert "authora" in tag_lookups
            assert "109" in tagged_scans
            inject_404_tag_101[0] = False

            author2 = Author(name="CircleB")
            session.add(author2)
            session.commit()
            rate_limit_slug[0] = True
            with pytest.raises(RuntimeError):
                await service.discover_author(author2.id)
            cached2 = session.scalar(
                select(ProviderAuthorIdentity).where(
                    ProviderAuthorIdentity.author_id == author2.id
                )
            )
            assert cached2 is None


async def test_recent_overlap_resumable_deep_failure_recovery_and_commit_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_settings(
        monkeypatch,
        catalog_recent_scan_overlap_pages=1,
        catalog_deep_scan_max_pages_per_run=2,
        catalog_deep_reconcile_interval_runs=3,
        catalog_detail_max_fetches_per_run=0,
    )
    pages_requested: list[int] = []
    fail_page_4 = [False]
    clock = FakeClock()
    pinned_item = {
        "id": 999,
        "media_id": "999",
        "english_title": "Pinned Old Work",
        "tag_ids": [101],
    }

    def make_page(p: int) -> list[dict]:
        if p == 1:
            return [
                pinned_item,
                {"id": 100, "media_id": "100", "english_title": "W100", "tag_ids": [101]},
            ]
        if p == 2:
            return [
                {"id": 100, "media_id": "100", "english_title": "W100", "tag_ids": [101]},
                {"id": 200, "media_id": "200", "english_title": "W200", "tag_ids": [101]},
            ]
        return [
            {
                "id": p * 100,
                "media_id": str(p * 100),
                "english_title": f"W{p * 100}",
                "tag_ids": [101],
            }
        ]

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/api/v2/tags/artist/authora" in url:
            return httpx.Response(200, json={"id": 101, "type": "artist", "name": "authora"})
        if "/api/v2/galleries/tagged" in url:
            p = int(request.url.params["page"])
            pages_requested.append(p)
            if p == 4 and fail_page_4[0]:
                return httpx.Response(500)
            return httpx.Response(200, json={"result": make_page(p), "num_pages": 5})
        return httpx.Response(404)

    with _make_memory_session() as session:
        author = Author(name="AuthorA")
        session.add(author)
        session.commit()

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = NhentaiProvider(
                "UA",
                max_search_pages=10,
                client=client,
                sleep=clock.sleep,
                clock=clock.now,
            )
            service = DiscoveryService(session, ProviderRegistry([provider]))

            # Run 1 (initial deep batch, budget=2): fetches [1, 2]; cursor=3.
            await service.discover_author(author.id)
            assert pages_requested == [1, 2]
            state = session.scalar(select(ProviderScanState))
            assert state.deep_cursor_page == 3
            assert state.last_deep_scan_at is None
            run1 = session.scalar(select(ProviderScanRun).order_by(ProviderScanRun.id.desc()))
            assert run1.reached_end is False
            assert run1.budget_truncated is True

            # Run 2: resumes with 1-page overlap -> [2, 3].
            pages_requested.clear()
            await service.discover_author(author.id)
            assert pages_requested == [2, 3]
            session.refresh(state)
            assert state.deep_cursor_page == 4

            # Run 3: resumes [3, 4], page 4 fails -> cursor stays 4 (NOT skipped!).
            pages_requested.clear()
            fail_page_4[0] = True
            res3 = await service.discover_author(author.id)
            assert pages_requested == [3, 4]
            assert res3["status"] == "partial"
            session.refresh(state)
            assert state.deep_cursor_page == 4
            assert state.last_deep_scan_at is None

            # Run 4: recovers, resumes [3, 4], cursor=5.
            fail_page_4[0] = False
            pages_requested.clear()
            await service.discover_author(author.id)
            assert pages_requested == [3, 4]
            session.refresh(state)
            assert state.deep_cursor_page == 5

            # Run 5: resumes [4, 5] -> reaches end -> true reached_end=True!
            pages_requested.clear()
            await service.discover_author(author.id)
            assert pages_requested == [4, 5]
            session.refresh(state)
            assert state.deep_cursor_page == 1
            assert state.deep_completed_once is True
            assert state.last_deep_scan_at is not None
            run5 = session.scalar(select(ProviderScanRun).order_by(ProviderScanRun.id.desc()))
            assert run5.reached_end is True

            events = list(session.scalars(select(CatalogEvent)))
            ext_ids = [e.external_id for e in events]
            assert sorted(ext_ids) == ["100", "200", "300", "400", "500", "999"]
            assert len(ext_ids) == len(set(ext_ids))

            # Recent poll after deep completion checks front overlap window [1].
            pages_requested.clear()
            await service.discover_author(author.id)
            assert pages_requested == [1]

            # Identity change resets scan cursor cleanly to 1.
            author.provider_query_names = {"nhentai": "AuthorA"}
            session.commit()
            await service.discover_author(author.id)
            session.refresh(state)
            assert state.deep_cursor_page == 3  # restarted from 1 -> [1, 2] -> cursor 3


async def test_db_commit_failure_rolls_back_cursor_and_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_settings(
        monkeypatch,
        catalog_deep_scan_max_pages_per_run=2,
        catalog_detail_max_fetches_per_run=0,
    )
    clock = FakeClock()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/api/v2/tags/artist/authora" in url:
            return httpx.Response(200, json={"id": 101, "type": "artist", "name": "authora"})
        if "/api/v2/galleries/tagged" in url:
            p = int(request.url.params["page"])
            item = {
                "id": p,
                "media_id": str(p),
                "english_title": f"W{p}",
                "tag_ids": [101],
            }
            return httpx.Response(200, json={"result": [item], "num_pages": 4})
        return httpx.Response(404)

    with _make_memory_session() as session:
        author = Author(name="AuthorA")
        session.add(author)
        session.commit()

        orig_commit = session.commit
        page_commit_calls = [0]

        def fail_second_page_commit() -> None:
            # Commits: 1 start_run, 2 start_provider, 3 resolve_targets, 4 page 1 -> fail page 2.
            page_commit_calls[0] += 1
            if page_commit_calls[0] == 5:
                raise RuntimeError("simulated sqlite disk full on page 2")
            orig_commit()

        monkeypatch.setattr(session, "commit", fail_second_page_commit)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = NhentaiProvider(
                "UA",
                max_search_pages=5,
                client=client,
                sleep=clock.sleep,
                clock=clock.now,
            )
            res = await DiscoveryService(
                session, ProviderRegistry([provider])
            ).discover_author(author.id)
            assert res["status"] == "partial"

        monkeypatch.setattr(session, "commit", orig_commit)
        state = session.scalar(select(ProviderScanState))
        works = list(session.scalars(select(Work)))
        assert len(works) == 1
        assert state.deep_cursor_page == 2


async def test_force_refresh_bypasses_caches_within_detail_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_settings(monkeypatch, catalog_detail_max_fetches_per_run=3)
    detail_calls: list[str] = []
    tag_lookups: list[str] = []
    clock = FakeClock()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/api/v2/tags/artist/" in url:
            tag_lookups.append(url)
            return httpx.Response(200, json={"id": 101, "type": "artist", "name": "authora"})
        if "/api/v2/galleries/tagged" in url:
            items = [
                {"id": i, "media_id": str(i), "english_title": f"W{i}", "tag_ids": [101]}
                for i in range(1, 6)
            ]
            return httpx.Response(200, json={"result": items, "num_pages": 1})
        if "/api/v2/galleries/" in url:
            gid = url.rstrip("/").split("/")[-1]
            detail_calls.append(gid)
            return httpx.Response(
                200,
                json={
                    "id": int(gid),
                    "media_id": gid,
                    "title": {"pretty": f"W{gid}"},
                    "tags": [{"id": 101, "type": "artist", "name": "authora"}],
                    "images": {"pages": [{"t": "j"}]},
                },
            )
        return httpx.Response(404)

    with _make_memory_session() as session:
        author = Author(name="AuthorA")
        session.add(author)
        session.commit()

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = NhentaiProvider(
                "UA", client=client, sleep=clock.sleep, clock=clock.now
            )
            service = DiscoveryService(session, ProviderRegistry([provider]))

            await service.discover_author(author.id)
            await service.discover_author(author.id)
            assert len(detail_calls) == 5

            detail_calls.clear()
            tag_lookups.clear()
            await service.discover_author(author.id, force_refresh=True)
            assert len(tag_lookups) == 1
            assert len(detail_calls) == 3
