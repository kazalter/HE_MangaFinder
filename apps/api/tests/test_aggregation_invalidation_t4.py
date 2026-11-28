from __future__ import annotations

import io
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
from PIL import Image, ImageDraw
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

import app.modules.catalog.invalidation as invalidation
from app.core.observability import capture_metrics
from app.db.base import Base
from app.db.models import (
    Author,
    AuthorAggregationState,
    CatalogEvent,
    MergeSuggestion,
    Work,
    WorkGroup,
)
from app.db.session import configure_sqlite_engine
from app.modules.authors.router import update_author_identity
from app.modules.authors.schemas import AuthorIdentityUpdate
from app.modules.catalog.aggregation import AggregationService
from app.modules.catalog.cover_maintenance import CoverFingerprintRefreshService
from app.modules.catalog.exclusions import WorkExclusionService
from app.modules.catalog.groups_router import (
    apply_group_metadata_update,
    reject_suggestion,
    split_work_group_member,
)
from app.modules.catalog.invalidation import (
    bump_author_aggregation,
    needs_author_aggregation,
)
from app.modules.catalog.operations_router import undo
from app.modules.catalog.repository import CatalogRepository
from app.modules.catalog.schemas import MetadataUpdate
from app.modules.catalog.service import DiscoveryService
from app.providers.base import DiscoveredWork, RemoteImage, SourceProvider
from app.providers.nhentai import NhentaiProvider
from app.providers.registry import ProviderRegistry


class FakeClock:
    def __init__(self, initial: float = 1000.0) -> None:
        self._now = initial

    def now(self) -> float:
        return self._now

    async def sleep(self, seconds: float) -> None:
        self._now += max(0.0, seconds)


@contextmanager
def _make_memory_session() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    configure_sqlite_engine(engine)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        yield session
    engine.dispose()


def _make_png(color: tuple[int, int, int], *, pattern: bool = False) -> bytes:
    img = Image.new("RGB", (160, 220), color)
    draw = ImageDraw.Draw(img)
    draw.rectangle((16, 16, 144, 96), fill=(255, 255, 255))
    draw.rectangle((28, 120, 132, 196), fill=(30, 30, 30) if pattern else (220, 60, 40))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class _StaticProvider(SourceProvider):
    def __init__(self, name: str, items: list[DiscoveredWork], cover: bytes | None = None) -> None:
        self.name = name
        self.display_name = name
        self.items = items
        self.cover = cover or _make_png((20, 90, 170))

    async def discover_by_author(
        self, author_name: str, *, author_names: set[str] | None = None
    ) -> list[DiscoveredWork]:
        return list(self.items)

    async def fetch_cover(self, external_id: str, url: str) -> RemoteImage:
        return RemoteImage(content=self.cover, content_type="image/png")


async def test_repeated_identical_scan_skips_aggregation_and_derived_updated_at() -> None:
    clock = FakeClock()
    cover = _make_png((30, 80, 140))

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/api/v2/tags/artist/" in url:
            return httpx.Response(200, json={"id": 101, "type": "artist", "name": "authora"})
        if "/api/v2/galleries/tagged" in url:
            return httpx.Response(
                200,
                json={
                    "result": [
                        {
                            "id": 91000 + idx,
                            "media_id": str(600 + idx),
                            "english_title": f"[AuthorA] Album {idx}",
                            "thumbnail": f"galleries/{600 + idx}/thumb.png",
                            "num_pages": 20 + idx,
                            "tag_ids": [101],
                        }
                        for idx in range(1, 6)
                    ],
                    "num_pages": 1,
                },
            )
        if "/api/v2/galleries/" in url:
            gid = int(url.rstrip("/").split("/")[-1])
            idx = gid - 91000
            return httpx.Response(
                200,
                json={
                    "id": gid,
                    "media_id": str(600 + idx),
                    "title": {"english": f"[AuthorA] Album {idx}"},
                    "tags": [{"id": 101, "type": "artist", "name": "authora"}],
                    "images": {
                        "pages": [{"t": "p"} for _ in range(20 + idx)],
                        "cover": {"t": "p"},
                    },
                    "num_pages": 20 + idx,
                    "upload_date": 1700000000,
                },
            )
        if "galleries/" in url:
            return httpx.Response(200, content=cover, headers={"content-type": "image/png"})
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

            with capture_metrics("t4.cold") as cold_metrics:
                await service.discover_author(author.id)
            cold = cold_metrics.snapshot()["counters"]
            assert cold.get("aggregation_executed", 0) == 5
            assert cold.get("candidate_loads", 0) == 1

            state = session.get(AuthorAggregationState, author.id)
            assert state is not None
            assert state.completed_revision == state.input_revision
            initial_rev = state.input_revision
            events_after_cold = len(list(session.scalars(select(CatalogEvent))))

            # recompute updates WorkGroup.updated_at, which must NOT trigger invalidation.
            group = session.scalar(select(WorkGroup).limit(1))
            assert group is not None
            AggregationService(session).recompute(group)
            session.commit()
            assert session.get(AuthorAggregationState, author.id).input_revision == initial_rev
            assert not needs_author_aggregation(session, author.id)

            # Second identical run must skip aggregation completely.
            with capture_metrics("t4.warm") as warm_metrics:
                await service.discover_author(author.id)
            warm = warm_metrics.snapshot()["counters"]
            assert warm.get("aggregation_executed", 0) == 0
            assert warm.get("aggregation_skipped", 0) >= 1
            assert warm.get("candidate_loads", 0) == 0
            assert warm.get("candidate_comparisons", 0) == 0
            assert len(list(session.scalars(select(CatalogEvent)))) == events_after_cold


async def test_new_work_matches_untouched_old_work_and_batches_candidates() -> None:
    clock = FakeClock()
    phase = {"has_new": False}
    cover = _make_png((45, 110, 180))

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/api/v2/tags/artist/" in url:
            return httpx.Response(200, json={"id": 101, "type": "artist", "name": "authora"})
        if "/api/v2/galleries/tagged" in url:
            items = [
                {
                    "id": 10001,
                    "media_id": "101",
                    "english_title": "[AuthorA] Untouched Moonlight Tale",
                    "num_pages": 24,
                    "tag_ids": [101],
                },
                {
                    "id": 10002,
                    "media_id": "102",
                    "english_title": "[AuthorA] Side Story",
                    "num_pages": 18,
                    "tag_ids": [101],
                },
            ]
            if phase["has_new"]:
                items.insert(
                    0,
                    {
                        "id": 10099,
                        "media_id": "199",
                        "english_title": "[AuthorA] Untouched Moonlight Tale [汉化]",
                        "num_pages": 24,
                        "tag_ids": [101],
                    },
                )
            return httpx.Response(200, json={"result": items, "num_pages": 1})
        if "/api/v2/galleries/" in url:
            gid = int(url.rstrip("/").split("/")[-1])
            media_id = "199" if gid == 10099 else ("101" if gid == 10001 else "102")
            title = (
                "[AuthorA] Untouched Moonlight Tale [汉化]"
                if gid == 10099
                else "[AuthorA] Untouched Moonlight Tale"
                if gid == 10001
                else "[AuthorA] Side Story"
            )
            return httpx.Response(
                200,
                json={
                    "id": gid,
                    "media_id": media_id,
                    "title": {"english": title},
                    "tags": [{"id": 101, "type": "artist", "name": "authora"}],
                    "images": {"pages": [{"t": "p"} for _ in range(24 if gid != 10002 else 18)]},
                    "num_pages": 24 if gid != 10002 else 18,
                },
            )
        if "wnacg.com/search/" in url:
            return httpx.Response(200, text="<html><body></body></html>")
        if "galleries/" in url:
            return httpx.Response(200, content=cover, headers={"content-type": "image/png"})
        return httpx.Response(404)

    with _make_memory_session() as session:
        author = Author(name="AuthorA")
        session.add(author)
        session.commit()

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            nh = NhentaiProvider("UA", client=client, sleep=clock.sleep, clock=clock.now)
            service = DiscoveryService(session, ProviderRegistry([nh]))

            await service.discover_author(author.id)
            assert len(list(session.scalars(select(WorkGroup)))) == 2

            # Untouched old work A must be loaded in candidate pool when B arrives.
            phase["has_new"] = True
            with capture_metrics("t4.incremental") as m:
                await service.discover_author(author.id)
            counters = m.snapshot()["counters"]
            assert counters.get("candidate_loads", 0) == 1
            assert counters.get("aggregation_executed", 0) == 1

            groups = list(session.scalars(select(WorkGroup).order_by(WorkGroup.id)))
            assert len(groups) == 2
            merged = next(g for g in groups if len(g.members) == 2)
            assert {m.work.title for m in merged.members} == {
                "[AuthorA] Untouched Moonlight Tale",
                "[AuthorA] Untouched Moonlight Tale [汉化]",
            }


async def test_invalidation_triggers_alias_split_constraint_exclusion_undo_cover_algo(
    monkeypatch,
) -> None:
    same_cover = _make_png((200, 70, 50), pattern=True)

    with _make_memory_session() as session:
        author = Author(name="AuthorMain", circle_names=[])
        session.add(author)
        session.commit()

        p1 = _StaticProvider(
            "src1",
            [
                DiscoveredWork(
                    external_id="w1",
                    title="[StudioAlpha] Secret Garden",
                    source_url="https://src1.test/w1",
                    cover_url="https://img.test/c1.png",
                    year=2025,
                    raw_metadata={"page_count": 20},
                ),
                DiscoveredWork(
                    external_id="w2",
                    title="Secret Garden",
                    source_url="https://src1.test/w2",
                    cover_url=None,
                    year=2025,
                    raw_metadata={"page_count": 20},
                ),
            ],
            cover=same_cover,
        )
        service = DiscoveryService(session, ProviderRegistry([p1]))
        await service.discover_author(author.id)
        assert not needs_author_aggregation(session, author.id)
        # Because StudioAlpha wasn't the author name yet and w2 has no cover, they stay separate.
        assert len(list(session.scalars(select(WorkGroup)))) == 2

        # 1. Identity update invalidates and allows re-reconciliation to merge them.
        update_author_identity(
            author.id,
            AuthorIdentityUpdate(name="StudioAlpha", aliases=["AuthorMain"]),
            session,
        )
        assert needs_author_aggregation(session, author.id)
        await service.discover_author(author.id)
        assert len(list(session.scalars(select(WorkGroup)))) == 1
        assert not needs_author_aggregation(session, author.id)

        # 2. Manual split invalidates author.
        group = session.scalar(select(WorkGroup))
        assert group is not None
        work2 = next(m.work for m in group.members if m.work.title == "Secret Garden")
        split_work_group_member(group.id, work2.id, session)
        assert needs_author_aggregation(session, author.id)

        # 3. Manual metadata update invalidates author.
        await service.discover_author(author.id, force_aggregation=True)
        assert not needs_author_aggregation(session, author.id)
        g_any = session.scalar(select(WorkGroup).limit(1))
        assert g_any is not None
        apply_group_metadata_update(g_any.id, MetadataUpdate(language="ja"), session)
        assert needs_author_aggregation(session, author.id)

        # 4. Pair constraint rejection invalidates author.
        g_list = list(session.scalars(select(WorkGroup).order_by(WorkGroup.id)))
        sug = MergeSuggestion(
            source_group_id=g_list[0].id,
            target_group_id=g_list[1].id,
            confidence=0.75,
            reasons=["test"],
        )
        session.add(sug)
        session.commit()
        await service.discover_author(author.id)
        assert not needs_author_aggregation(session, author.id)
        reject_suggestion(sug.id, session)
        assert needs_author_aggregation(session, author.id)

        # 5. Exclusion + Undo (un-exclude) invalidate author.
        await service.discover_author(author.id)
        assert not needs_author_aggregation(session, author.id)
        from app.modules.catalog.groups_router import exclude_work_group_for_author

        exclude_work_group_for_author(g_list[1].id, author.id, session)
        assert needs_author_aggregation(session, author.id)
        from app.db.models import CatalogOperation

        op = session.scalar(
            select(CatalogOperation)
            .where(CatalogOperation.kind == "exclude")
            .order_by(CatalogOperation.id.desc())
        )
        assert op is not None
        await service.discover_author(author.id)
        assert not needs_author_aggregation(session, author.id)
        undo(op.id, session)
        assert needs_author_aggregation(session, author.id)

        # 6. Cover maintenance refresh invalidates affected authors.
        await service.discover_author(author.id)
        assert not needs_author_aggregation(session, author.id)
        w1 = session.scalar(select(Work).where(Work.title == "[StudioAlpha] Secret Garden"))
        assert w1 is not None and w1.fingerprint is not None
        w1.fingerprint.cover_fingerprint = None
        w1.fingerprint.cover_hash = None
        session.commit()
        res = await CoverFingerprintRefreshService(
            session, ProviderRegistry([p1])
        ).run(batch_size=10)
        assert res["refreshed"] == 1
        assert needs_author_aggregation(session, author.id)

        # 7. Algorithm version upgrade triggers invalidation.
        await service.discover_author(author.id)
        assert not needs_author_aggregation(session, author.id)
        monkeypatch.setattr(invalidation, "AGGREGATION_ALGORITHM_VERSION", "agg-v99")
        assert needs_author_aggregation(session, author.id)
        await service.discover_author(author.id)
        state = session.get(AuthorAggregationState, author.id)
        assert state is not None
        assert state.algorithm_version == "agg-v99"
        assert not needs_author_aggregation(session, author.id)


async def test_shared_work_invalidates_all_authors_and_exclusion() -> None:
    with _make_memory_session() as session:
        a1 = Author(name="CoAuthor1")
        a2 = Author(name="CoAuthor2")
        session.add_all([a1, a2])
        session.commit()

        repo = CatalogRepository(session)
        shared_item = DiscoveredWork(
            external_id="shared-1",
            title="Shared Book",
            source_url="https://wnacg.com/photos-index-aid-1.html",
        )
        s1 = DiscoveryService(session, ProviderRegistry([_StaticProvider("wnacg", [shared_item])]))
        s2 = DiscoveryService(session, ProviderRegistry([_StaticProvider("wnacg", [shared_item])]))
        await s1.discover_author(a1.id)
        await s2.discover_author(a2.id)
        # Linking a2 onto existing work shared-1 dirties a1 as well; reconcile a1 cleanly.
        assert needs_author_aggregation(session, a1.id)
        assert not needs_author_aggregation(session, a2.id)
        await s1.discover_author(a1.id)
        assert not needs_author_aggregation(session, a1.id)
        assert not needs_author_aggregation(session, a2.id)

        # Updating shared work via repo invalidates BOTH authors.
        updated_item = DiscoveredWork(
            external_id="shared-1",
            title="Shared Book [DL]",
            source_url="https://wnacg.com/photos-index-aid-1.html",
        )
        repo.upsert_with_result(a1.id, "wnacg", updated_item)
        session.commit()
        assert needs_author_aggregation(session, a1.id)
        assert needs_author_aggregation(session, a2.id)

        await s1.discover_author(a1.id)
        await s2.discover_author(a2.id)
        assert not needs_author_aggregation(session, a1.id)
        assert not needs_author_aggregation(session, a2.id)

        # Excluding shared work from a1 still invalidates BOTH a1 and a2.
        shared_work = session.scalar(select(Work))
        assert shared_work is not None
        WorkExclusionService(session).exclude_work(a1, shared_work)
        session.commit()
        assert needs_author_aggregation(session, a1.id)
        assert needs_author_aggregation(session, a2.id)


async def test_concurrent_bump_and_interruption_prevent_marking_complete(
    tmp_path: Path, monkeypatch
) -> None:
    db_file = tmp_path / "t4_concurrent.db"
    engine = create_engine(
        f"sqlite:///{db_file}", connect_args={"check_same_thread": False}
    )
    configure_sqlite_engine(engine)
    Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine, expire_on_commit=False)

    try:
        with Factory() as session:
            author = Author(name="RaceAuthor")
            session.add(author)
            session.commit()
            author_id = author.id

            p = _StaticProvider(
                "src1",
                [
                    DiscoveredWork(
                        external_id="r1",
                        title="Race Book 1",
                        source_url="https://src1.test/r1",
                    )
                ],
            )

            orig_assign = AggregationService.assign
            bumped_once = [False]

            async def racing_assign(
                self: AggregationService, work: Work, auth: Author
            ) -> WorkGroup:
                res = await orig_assign(self, work, auth)
                if not bumped_once[0]:
                    bumped_once[0] = True
                    # Concurrent modification happens in same transaction/session.
                    bump_author_aggregation(
                        self.session, auth.id, reason="concurrent_edit", full_reconcile=True
                    )
                return res

            monkeypatch.setattr(AggregationService, "assign", racing_assign)
            service = DiscoveryService(session, ProviderRegistry([p]))
            await service.discover_author(author_id)

            state = session.get(AuthorAggregationState, author_id)
            assert state is not None
            assert state.input_revision > state.completed_revision
            assert needs_author_aggregation(session, author_id)

        # Restore normal assign; next discovery sees dirty state and finishes cleanly.
        monkeypatch.setattr(AggregationService, "assign", orig_assign)
        with Factory() as session:
            service = DiscoveryService(
                session,
                ProviderRegistry(
                    [
                        _StaticProvider(
                            "src1",
                            [
                                DiscoveredWork(
                                    external_id="r1",
                                    title="Race Book 1",
                                    source_url="https://src1.test/r1",
                                )
                            ],
                        )
                    ]
                ),
            )
            await service.discover_author(author_id)
            state = session.get(AuthorAggregationState, author_id)
            assert state is not None
            assert state.completed_revision == state.input_revision
            assert not needs_author_aggregation(session, author_id)

            # Force re-aggregation always executes even when clean.
            with capture_metrics("t4.force") as forced_m:
                await service.discover_author(author_id, force_aggregation=True)
            assert forced_m.snapshot()["counters"].get("aggregation_executed", 0) >= 1
    finally:
        engine.dispose()
