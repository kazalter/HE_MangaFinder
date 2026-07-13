import io

import httpx
from PIL import Image
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.models import Author, MergeSuggestion, WorkGroup
from app.modules.catalog.aggregation import (
    AggregationService,
    CoverHasher,
    extract_variant_labels,
    identity_number_signature,
    normalize_title,
)
from app.modules.catalog.repository import CatalogRepository
from app.providers.base import DiscoveredWork


def test_normalizes_upload_variants_but_keeps_sequel_identity() -> None:
    author = "mignon"

    assert normalize_title(
        "(C106) [MIGNON WORKS (mignon)] ぬぎおなか [白杨汉化组] [無修正]",
        author,
    ) == normalize_title("[MIGNON WORKS (mignon)] ぬぎおなか [DL版]", author)
    assert normalize_title("测试漫画", "作者") != normalize_title("测试漫画 续", "作者")
    assert normalize_title(
        "(AC2) [MIGNON WORKS (mignon)] JK×ONAKA #03 (オリジナル)", author
    ) == "jk x onaka 03"
    assert normalize_title("ONAKA SUMMER 2 [Decensored]", author) == "onaka summer 2"
    assert normalize_title("濡れおなか総集編V3", author) == "濡れおなか総集編"

    assert identity_number_signature("jk x onaka 02 jk x 小腹 02") == (2,)
    assert identity_number_signature("作品 01") == (1,)
    assert identity_number_signature("作品 2025 1080p") == ()


def test_common_franchise_suffix_does_not_create_merge_candidate() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        author = Author(name="ラマンダ")
        session.add(author)
        session.flush()
        catalog = CatalogRepository(session)
        aggregation = AggregationService(session)
        titles = [
            "[ラマンダ] アコちゃんサンタのプレゼント (ブルーアーカイブ)",
            "[ラマンダ] サンタアスナとカリンのプレゼント (ブルーアーカイブ)",
        ]
        groups = []
        for index, title in enumerate(titles):
            work = catalog.upsert(
                author.id,
                "wnacg",
                DiscoveredWork(
                    external_id=f"christmas-{index}",
                    title=title,
                    source_url=f"https://example.test/{index}",
                ),
            )
            groups.append(aggregation.assign_without_cover(work, author))

        assert groups[0].id != groups[1].id
        assert list(session.scalars(select(MergeSuggestion))) == []

    labels = extract_variant_labels("作品 [白杨汉化组] [AI無修正]", "zh-hans")
    assert "白杨汉化组" in labels
    assert "无码/无修正" in labels
    assert "AI 处理" in labels
    assert "简体中文" in labels


def test_groups_versions_and_supports_reversible_manual_correction() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        author = Author(name="mignon")
        session.add(author)
        session.flush()
        catalog = CatalogRepository(session)
        aggregation = AggregationService(session)

        first = catalog.upsert(
            author.id,
            "wnacg",
            DiscoveredWork(
                external_id="1",
                title="[MIGNON WORKS (mignon)] ぬぎおなか [白杨汉化组]",
                source_url="https://example.test/1",
                raw_metadata={"page_count": 30},
            ),
        )
        first_group = aggregation.assign_without_cover(first, author)
        second = catalog.upsert(
            author.id,
            "wnacg",
            DiscoveredWork(
                external_id="2",
                title="(C106) [MIGNON WORKS (mignon)] ぬぎおなか [無修正]",
                source_url="https://example.test/2",
                raw_metadata={"page_count": 30},
            ),
        )
        second_group = aggregation.assign_without_cover(second, author)
        sequel = catalog.upsert(
            author.id,
            "wnacg",
            DiscoveredWork(
                external_id="3",
                title="[MIGNON WORKS (mignon)] ぬぎおなか 续",
                source_url="https://example.test/3",
            ),
        )
        sequel_group = aggregation.assign_without_cover(sequel, author)

        assert first_group.id == second_group.id
        assert len(first_group.members) == 2
        assert sequel_group.id != first_group.id
        assert len(list(session.scalars(select(WorkGroup)))) == 2

        split_group = aggregation.split_member(first_group, second.id)
        assert split_group.id != first_group.id
        assert split_group.members[0].is_manual is True

        merged = aggregation.merge_groups(first_group, split_group)
        assert len(merged.members) == 2
        assert all(member.is_manual for member in merged.members if member.work_id == second.id)


def test_numbered_works_only_group_when_the_number_signature_matches() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        author = Author(name="mignon")
        session.add(author)
        session.flush()
        catalog = CatalogRepository(session)
        aggregation = AggregationService(session)

        first = catalog.upsert(
            author.id,
            "hanimeone",
            DiscoveredWork(
                external_id="number-1",
                title="JK x ONAKA #01 [汉化组甲]",
                source_url="https://example.test/number-1",
            ),
        )
        first_group = aggregation.assign_without_cover(first, author)
        same_number = catalog.upsert(
            author.id,
            "wnacg",
            DiscoveredWork(
                external_id="number-01-version",
                title="JK x ONAKA #1 [汉化组乙]",
                source_url="https://example.test/number-01-version",
            ),
        )
        same_group = aggregation.assign_without_cover(same_number, author)
        second = catalog.upsert(
            author.id,
            "hanimeone",
            DiscoveredWork(
                external_id="number-2",
                title="JK x ONAKA #02 [汉化组甲]",
                source_url="https://example.test/number-2",
            ),
        )
        second_group = aggregation.assign_without_cover(second, author)
        unnumbered = catalog.upsert(
            author.id,
            "hanimeone",
            DiscoveredWork(
                external_id="unnumbered",
                title="JK x ONAKA [汉化组甲]",
                source_url="https://example.test/unnumbered",
            ),
        )
        unnumbered_group = aggregation.assign_without_cover(unnumbered, author)

        assert same_group.id == first_group.id
        assert second_group.id != first_group.id
        assert unnumbered_group.id not in {first_group.id, second_group.id}
        assert len(list(session.scalars(select(WorkGroup)))) == 3


async def test_refresh_splits_an_existing_automatic_group_with_different_numbers() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        author = Author(name="mignon")
        session.add(author)
        session.flush()
        catalog = CatalogRepository(session)
        aggregation = AggregationService(session)
        first = catalog.upsert(
            author.id,
            "hanimeone",
            DiscoveredWork(
                external_id="old-1",
                title="Nure Onaka 1 [汉化组]",
                source_url="https://example.test/old-1",
            ),
        )
        second = catalog.upsert(
            author.id,
            "hanimeone",
            DiscoveredWork(
                external_id="old-2",
                title="Nure Onaka 2 [汉化组]",
                source_url="https://example.test/old-2",
            ),
        )
        first_group = aggregation.assign_without_cover(first, author)
        second_group = aggregation.assign_without_cover(second, author)
        aggregation.merge_groups(
            first_group, second_group, manual=False, method="legacy_title_fuzzy"
        )

        refreshed_group = await aggregation.assign(first, author)

        assert refreshed_group.id != second.group_membership.group_id
        assert len(list(session.scalars(select(WorkGroup)))) == 2


async def test_strong_cover_merges_cross_language_titles_but_never_other_numbers() -> None:
    image = Image.new("RGB", (30, 30), "white")
    for x in range(15):
        for y in range(30):
            image.putpixel((x, y), (20, 20, 20))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=buffer.getvalue())
        )
    )
    hasher = CoverHasher(client)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        author = Author(name="mignon")
        session.add(author)
        session.flush()
        catalog = CatalogRepository(session)
        aggregation = AggregationService(session, hasher)

        japanese = catalog.upsert(
            author.id,
            "wnacg",
            DiscoveredWork(
                external_id="japanese-2",
                title="(C102) [MIGNON WORKS (mignon)] 透けおなか2",
                source_url="https://example.test/japanese-2",
                cover_url="https://example.test/japanese-2.png",
            ),
        )
        japanese_group = await aggregation.assign(japanese, author)
        romanized = catalog.upsert(
            author.id,
            "hanimeone",
            DiscoveredWork(
                external_id="romanized-2",
                title="Suke Onaka 02 [汉化组]",
                source_url="https://example.test/romanized-2",
                cover_url="https://example.test/romanized-2.png",
            ),
        )
        romanized_group = await aggregation.assign(romanized, author)
        other_number = catalog.upsert(
            author.id,
            "hanimeone",
            DiscoveredWork(
                external_id="romanized-3",
                title="Suke Onaka #03 [汉化组]",
                source_url="https://example.test/romanized-3",
                cover_url="https://example.test/romanized-3.png",
            ),
        )
        other_group = await aggregation.assign(other_number, author)

        assert romanized_group.id == japanese_group.id
        assert other_group.id != japanese_group.id

    await client.aclose()


async def test_cover_hasher_is_visual_not_url_based() -> None:
    image = Image.new("RGB", (30, 30), "white")
    for x in range(15):
        for y in range(30):
            image.putpixel((x, y), (20, 20, 20))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=buffer.getvalue())
        )
    )
    hasher = CoverHasher(client)

    assert await hasher.hash_url("https://one.example/cover.png") == await hasher.hash_url(
        "https://another.example/resized.png"
    )
    await client.aclose()
