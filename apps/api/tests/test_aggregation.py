import io
from datetime import UTC, datetime

import httpx
from PIL import Image, ImageDraw
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
from app.modules.catalog.cover_fingerprint import (
    compare_fingerprints,
    fingerprint_image,
)
from app.modules.catalog.group_repository import WorkGroupRepository
from app.modules.catalog.groups_router import _summary
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


def test_group_summary_includes_subscribed_authors() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        author = Author(name="测试作者")
        session.add(author)
        session.flush()
        author_id = author.id
        work = CatalogRepository(session).upsert(
            author.id,
            "test",
            DiscoveredWork(
                external_id="author-summary",
                title="测试作品",
                source_url="https://example.test/author-summary",
            ),
        )
        AggregationService(session).assign_without_cover(work, author)
        session.commit()
        session.expunge_all()

        group = WorkGroupRepository(session).list_groups()[0]
        summary = _summary(group)

        assert [(item.id, item.name) for item in summary.authors] == [
            (author_id, "测试作者")
        ]


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
                source_updated_at=datetime(2023, 11, 19, tzinfo=UTC),
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
                source_updated_at=datetime(2026, 7, 16, tzinfo=UTC),
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
        assert first_group.first_source_at == datetime(2023, 11, 19, tzinfo=UTC)
        assert first_group.latest_source_at == datetime(2026, 7, 16, tzinfo=UTC)
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


def test_exact_source_alias_still_groups_translated_titles() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        author = Author(name="作者")
        session.add(author)
        session.flush()
        catalog = CatalogRepository(session)
        aggregation = AggregationService(session)
        original = catalog.upsert(
            author.id,
            "source-a",
            DiscoveredWork(
                external_id="original",
                title="海のお腹 2",
                source_url="https://example.test/original",
                raw_metadata={"altTitles": ["Ocean Belly 2"]},
            ),
        )
        original_group = aggregation.assign_without_cover(original, author)
        translation = catalog.upsert(
            author.id,
            "source-b",
            DiscoveredWork(
                external_id="translation",
                title="Ocean Belly 2",
                source_url="https://example.test/translation",
            ),
        )
        translation_group = aggregation.assign_without_cover(translation, author)

        assert translation_group.id == original_group.id
        assert len(original_group.members) == 2


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


def test_cover_fingerprint_matches_same_art_after_aggressive_center_crop() -> None:
    wide = Image.new("RGB", (500, 270), "#d8b195")
    draw = ImageDraw.Draw(wide)
    draw.rectangle((170, 15, 330, 255), fill="#253f72")
    draw.ellipse((205, 45, 295, 135), fill="#f2d1ba")
    draw.line((180, 230, 320, 150), fill="#ecb343", width=15)
    portrait = wide.crop((149, 0, 351, 270)).resize((300, 400))

    comparison = compare_fingerprints(
        fingerprint_image(wide), fingerprint_image(portrait)
    )

    assert comparison is not None
    assert comparison.mode == "crop"
    assert comparison.distance <= 8
    assert comparison.reliable_negative is True


def test_cover_fingerprint_keeps_unrelated_art_separate() -> None:
    first = Image.new("RGB", (500, 270), "white")
    second = Image.new("RGB", (300, 400), "#18294f")
    first_draw = ImageDraw.Draw(first)
    second_draw = ImageDraw.Draw(second)
    for offset in range(0, 500, 35):
        first_draw.line((offset, 0, 500 - offset, 270), fill="#bb3f32", width=9)
    for offset in range(0, 400, 30):
        second_draw.ellipse((30, offset, 270, offset + 18), fill="#e6c851")

    comparison = compare_fingerprints(
        fingerprint_image(first), fingerprint_image(second)
    )

    assert comparison is not None
    assert comparison.distance >= 23
    assert comparison.reliable_negative is True


def test_legacy_cover_hash_never_proves_covers_are_different() -> None:
    comparison = compare_fingerprints(
        None,
        None,
        left_legacy="0000000000000000",
        right_legacy="ffffffffffffffff",
    )

    assert comparison is not None and comparison.distance == 64
    assert comparison.mode == "legacy"
    assert comparison.reliable_negative is False


def test_prepared_cover_and_title_matching_preserve_exact_scores() -> None:
    from app.modules.catalog.cover_fingerprint import (
        compare_prepared_fingerprints,
        prepare_cover_fingerprint,
    )
    from app.modules.catalog.title_identity import (
        best_identity_similarity,
        best_prepared_similarity,
        prepare_identity_text,
    )

    first = Image.new("RGB", (500, 270), "white")
    second = Image.new("RGB", (300, 400), "#18294f")
    fp1 = fingerprint_image(first)
    fp2 = fingerprint_image(second)
    assert compare_prepared_fingerprints(
        prepare_cover_fingerprint(fp1, legacy="0000000000000000"),
        prepare_cover_fingerprint(fp2, legacy="000000000000000f"),
    ) == compare_fingerprints(
        fp1, fp2, left_legacy="0000000000000000", right_legacy="000000000000000f"
    )

    left_names = {"summer holiday 2", "サマーホリデー 2"}
    right_names = {"summer holiday 02", "unrelated long title"}
    expected = best_identity_similarity(left_names, right_names)
    actual = best_prepared_similarity(
        tuple(prepare_identity_text(v) for v in left_names),
        tuple(prepare_identity_text(v) for v in right_names),
    )
    assert actual == expected


def test_list_group_reads_matches_orm_summary_and_suggestions_batch() -> None:
    from app.modules.catalog.groups_router import _summary, list_suggestions

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
                external_id="w1",
                title="[mignon] Work One",
                source_url="https://example.test/w1",
                cover_url="https://t4.qy0.ru/1.jpg",
                tags=["tag-a"],
            ),
        )
        second = catalog.upsert(
            author.id,
            "local",
            DiscoveredWork(
                external_id="w2",
                title="[mignon] Work Two",
                source_url="https://example.test/w2",
                cover_url="https://cdn.example/2.jpg",
            ),
        )
        g1 = aggregation.assign_without_cover(first, author)
        g2 = aggregation.assign_without_cover(second, author)
        session.add(
            MergeSuggestion(
                source_group_id=g1.id,
                target_group_id=g2.id,
                confidence=0.72,
                reasons=["test"],
            )
        )
        session.commit()

        repo = WorkGroupRepository(session)
        assert repo.list_group_reads() == [_summary(group) for group in repo.list_groups()]
        assert repo.list_group_reads(author.id) == [
            _summary(group) for group in repo.list_groups(author.id)
        ]
        suggestions = list_suggestions(session, status="pending")
        assert len(suggestions) == 1
        assert suggestions[0].source_group == _summary(g1)
        assert suggestions[0].target_group == _summary(g2)


def test_compare_prepared_fingerprints_respects_max_distance_with_legacy() -> None:
    from app.modules.catalog.cover_fingerprint import (
        compare_prepared_fingerprints,
        prepare_cover_fingerprint,
    )

    first = Image.new("RGB", (400, 560), "white")
    second = Image.new("RGB", (400, 560), "#18294f")
    ImageDraw.Draw(first).line((0, 0, 400, 560), fill="#bb3f32", width=18)
    ImageDraw.Draw(second).ellipse((40, 80, 360, 480), fill="#e6c851")
    fp1 = fingerprint_image(first)
    fp2 = fingerprint_image(second)
    full = compare_fingerprints(
        fp1, fp2, left_legacy="0000000000000000", right_legacy="0000000000000000"
    )
    assert full is not None and full.distance > 13
    # Even when legacy dHash matches (distance 0), max_distance=13 must not fall back to legacy
    # when v2 variants existed and proved distance > 13.
    assert (
        compare_prepared_fingerprints(
            prepare_cover_fingerprint(fp1, legacy="0000000000000000"),
            prepare_cover_fingerprint(fp2, legacy="0000000000000000"),
            max_distance=13,
        )
        is None
    )


def test_chapters_never_merge_and_normalization_preserves_word_parts() -> None:
    assert normalize_title("Love Story Rev2 [中文] [DL]", "Author") == "love story rev2"
    assert normalize_title("Scandal Night [Scan]", "Author") == "scandal night"
    assert normalize_title("Chinese Dress [English]", "Author") == "chinese dress"
    assert normalize_title("My Manga (全12話)", "Author") == "my manga"
    assert normalize_title("My Manga 第1話", "Author") == normalize_title(
        "My Manga Ch. 1", "Author"
    )
    assert normalize_title("My Manga 第1話", "Author") != normalize_title(
        "My Manga 第2話", "Author"
    )

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        author = Author(name="SerialAuthor")
        session.add(author)
        session.flush()
        catalog = CatalogRepository(session)
        aggregation = AggregationService(session)
        ch1 = catalog.upsert(
            author.id,
            "wnacg",
            DiscoveredWork(
                external_id="ch-1",
                title="Serial Tale 第1話 [汉化]",
                source_url="https://example.test/ch1",
            ),
        )
        ch1_alt = catalog.upsert(
            author.id,
            "nhentai",
            DiscoveredWork(
                external_id="ch-1-en",
                title="Serial Tale Ch. 1 [English]",
                source_url="https://example.test/ch1-en",
            ),
        )
        ch2 = catalog.upsert(
            author.id,
            "wnacg",
            DiscoveredWork(
                external_id="ch-2",
                title="Serial Tale 第2話 [汉化]",
                source_url="https://example.test/ch2",
            ),
        )
        g1 = aggregation.assign_without_cover(ch1, author)
        g1_alt = aggregation.assign_without_cover(ch1_alt, author)
        g2 = aggregation.assign_without_cover(ch2, author)

        assert g1.id == g1_alt.id
        assert g2.id != g1.id
        assert len(list(session.scalars(select(WorkGroup)))) == 2


async def test_one_sided_number_never_auto_merges_and_suggests_on_strong_cover() -> None:
    cover = Image.new("RGB", (300, 420), "#f3ede4")
    draw = ImageDraw.Draw(cover)
    draw.rectangle((40, 50, 260, 350), fill="#224488")
    draw.ellipse((90, 90, 210, 210), fill="#eeaa44")
    fp = fingerprint_image(cover)

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        author = Author(name="LongTitleAuthor")
        session.add(author)
        session.flush()
        catalog = CatalogRepository(session)
        aggregation = AggregationService(session)
        w1 = catalog.upsert(
            author.id,
            "wnacg",
            DiscoveredWork(
                external_id="one-1",
                title="Extremely Long Doujinshi Title About Everyday Life #01",
                source_url="https://example.test/one-1",
                year=2025,
                raw_metadata={"page_count": 28},
            ),
        )
        w2 = catalog.upsert(
            author.id,
            "nhentai",
            DiscoveredWork(
                external_id="one-none",
                title="Extremely Long Doujinshi Title About Everyday Life",
                source_url="https://example.test/one-none",
                year=2025,
                raw_metadata={"page_count": 28},
            ),
        )
        g1 = await aggregation.assign(w1, author)
        g2 = await aggregation.assign(w2, author)
        assert g1.id != g2.id

        # Even with identical strong cover, one-sided missing number only creates a suggestion.
        w1.fingerprint.cover_fingerprint = fp
        w2.fingerprint.cover_fingerprint = fp
        session.flush()
        await aggregation.reaggregate_author(author)
        assert w1.group_membership.group_id != w2.group_membership.group_id
        suggestions = list(session.scalars(select(MergeSuggestion)))
        assert len(suggestions) == 1
        assert any("缺少作品编号" in reason for reason in suggestions[0].reasons)


async def test_semantic_markers_and_extreme_page_mismatch_block_auto_merge() -> None:
    cover = Image.new("RGB", (300, 420), "#e8dcd0")
    draw = ImageDraw.Draw(cover)
    draw.rectangle((30, 40, 270, 380), fill="#335577")
    draw.line((30, 40, 270, 380), fill="#ffdd66", width=16)
    fp = fingerprint_image(cover)

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        author = Author(name="SemanticAuthor")
        session.add(author)
        session.flush()
        catalog = CatalogRepository(session)
        aggregation = AggregationService(session)
        titles = [
            ("s-base", "Very Long Doujinshi Title About School Days", 24),
            ("s-comp", "Very Long Doujinshi Title About School Days 総集編", 24),
            ("s-seq", "Very Long Doujinshi Title About School Days 续", 24),
            ("s-spin", "Very Long Doujinshi Title About School Days 番外編", 24),
            ("s-samp", "Very Long Doujinshi Title About School Days 体験版", 24),
            ("s-front", "Long Split Tale 前編", 24),
            ("s-back", "Long Split Tale 後編", 24),
            ("s-rom1", "Long Split Tale Part I", 24),
            ("s-rom2", "Long Split Tale Part II", 24),
        ]
        works = []
        for ext_id, title, pages in titles:
            w = catalog.upsert(
                author.id,
                "wnacg",
                DiscoveredWork(
                    external_id=ext_id,
                    title=title,
                    source_url=f"https://example.test/{ext_id}",
                    year=2025,
                    raw_metadata={"page_count": pages},
                ),
            )
            agg_fp = aggregation._upsert_fingerprint(w, author.name)
            agg_fp.cover_fingerprint = fp
            works.append(w)

        await aggregation.reaggregate_author(author)
        group_ids = {w.group_membership.group_id for w in works}
        assert len(group_ids) == len(titles)

        # Identical core title but 2p excerpt vs 135p anthology must stay separate and suggest.
        excerpt = catalog.upsert(
            author.id,
            "nhentai",
            DiscoveredWork(
                external_id="ex-2p",
                title="Shared Anthology Work",
                source_url="https://example.test/ex-2p",
                raw_metadata={"page_count": 2},
            ),
        )
        anthology = catalog.upsert(
            author.id,
            "nhentai",
            DiscoveredWork(
                external_id="ex-135p",
                title="Shared Anthology Work",
                source_url="https://example.test/ex-135p",
                raw_metadata={"page_count": 135},
            ),
        )
        g_ex = await aggregation.assign(excerpt, author)
        g_an = await aggregation.assign(anthology, author)
        assert g_ex.id != g_an.id
        sug = session.scalar(
            select(MergeSuggestion).where(
                MergeSuggestion.source_group_id == g_an.id,
                MergeSuggestion.target_group_id == g_ex.id,
            )
        )
        assert sug is not None


async def test_pair_constraint_blocks_auto_remerge_across_cover_and_anchor_drift() -> None:
    from app.modules.agent_review.repository import AgentReviewRepository
    from app.modules.catalog.pair_identity import candidate_key

    cover = Image.new("RGB", (300, 420), "#f1e8dc")
    ImageDraw.Draw(cover).rectangle((40, 60, 260, 340), fill="#882244")
    fp = fingerprint_image(cover)

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        author = Author(name="ConstraintAuthor")
        session.add(author)
        session.flush()
        catalog = CatalogRepository(session)
        aggregation = AggregationService(session)
        w1 = catalog.upsert(
            author.id,
            "wnacg",
            DiscoveredWork(
                external_id="c-500",
                title="Distinct Story Alpha",
                source_url="https://example.test/c500",
                year=2025,
                raw_metadata={"page_count": 24},
            ),
        )
        w2 = catalog.upsert(
            author.id,
            "wnacg",
            DiscoveredWork(
                external_id="c-600",
                title="Distinct Story Beta",
                source_url="https://example.test/c600",
                year=2025,
                raw_metadata={"page_count": 24},
            ),
        )
        g1 = await aggregation.assign(w1, author)
        g2 = await aggregation.assign(w2, author)
        AgentReviewRepository(session).constrain_pair(
            candidate_key(g1, g2), origin="human_rejection"
        )
        # Even if a smaller external_id edition joins g1 and covers are identical, never merge.
        w1_extra = catalog.upsert(
            author.id,
            "nhentai",
            DiscoveredWork(
                external_id="a-001",
                title="Distinct Story Alpha [汉化]",
                source_url="https://example.test/a001",
                year=2025,
                raw_metadata={"page_count": 24},
            ),
        )
        await aggregation.assign(w1_extra, author)
        assert w1_extra.group_membership.group_id == g1.id

        w1.fingerprint.cover_fingerprint = fp
        w2.fingerprint.cover_fingerprint = fp
        session.flush()
        await aggregation.reaggregate_author(author)
        assert w1.group_membership.group_id != w2.group_membership.group_id


async def test_aliases_circles_alttitles_context_and_trad_simp_folding() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        author = Author(
            name="AuthorMain",
            aliases=["PenAlias"],
            circle_names=["CircleStudio"],
        )
        session.add(author)
        session.flush()
        catalog = CatalogRepository(session)
        aggregation = AggregationService(session)

        w1 = catalog.upsert(
            author.id,
            "nhentai",
            DiscoveredWork(
                external_id="nh-ctx",
                title="[CircleStudio] Ocean Story (Blue Archive)",
                source_url="https://example.test/nh-ctx",
                raw_metadata={
                    "altTitles": ["[PenAlias] 海の物語 (ブルーアーカイブ) [Digital]"]
                },
            ),
        )
        w2 = catalog.upsert(
            author.id,
            "wnacg",
            DiscoveredWork(
                external_id="wn-ctx",
                title="(同人誌) (C105) [PenAlias] 海の物語 (ブルーアーカイブ) [中文]",
                source_url="https://example.test/wn-ctx",
            ),
        )
        g1 = await aggregation.assign(w1, author)
        g2 = await aggregation.assign(w2, author)
        assert g1.id == g2.id

        # Trad/Simp folding (Option A): uncorroborated -> suggest; corroborated -> merge
        t_only = catalog.upsert(
            author.id,
            "wnacg",
            DiscoveredWork(
                external_id="ts-1",
                title="[CircleStudio] 美麗新世界 [漢化]",
                source_url="https://example.test/ts1",
            ),
        )
        s_only = catalog.upsert(
            author.id,
            "nhentai",
            DiscoveredWork(
                external_id="ts-2",
                title="[PenAlias] 美丽新世界 [汉化]",
                source_url="https://example.test/ts2",
            ),
        )
        gt = await aggregation.assign(t_only, author)
        gs = await aggregation.assign(s_only, author)
        assert gt.id != gs.id
        sug = session.scalar(
            select(MergeSuggestion).where(
                MergeSuggestion.source_group_id == gs.id,
                MergeSuggestion.target_group_id == gt.id,
            )
        )
        assert sug is not None and any("繁简" in r for r in sug.reasons)

        # Now provide matching page counts -> auto-merges on reaggregate.
        t_only.sources[0].raw_metadata = {"page_count": 24}
        s_only.sources[0].raw_metadata = {"page_count": 24}
        await aggregation.reaggregate_author(author)
        assert t_only.group_membership.group_id == s_only.group_membership.group_id


async def test_low_entropy_cover_guard_and_top_k_fuzzy_cover_and_order_independence() -> None:
    solid_white = fingerprint_image(Image.new("RGB", (320, 440), "white"))
    cover_a = Image.new("RGB", (320, 440), "#f0e0d0")
    ImageDraw.Draw(cover_a).rectangle((30, 30, 290, 410), fill="#114477")
    cover_b = Image.new("RGB", (320, 440), "#d0e8f0")
    ImageDraw.Draw(cover_b).ellipse((40, 80, 280, 360), fill="#aa3322")
    fp_a = fingerprint_image(cover_a)
    fp_b = fingerprint_image(cover_b)

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        author = Author(name="CoverOrderAuthor")
        session.add(author)
        session.flush()
        catalog = CatalogRepository(session)
        aggregation = AggregationService(session)

        # Solid white covers on distinct titles must NOT merge despite 0 Hamming distance.
        u1 = catalog.upsert(
            author.id,
            "wnacg",
            DiscoveredWork(
                external_id="low-1",
                title="Blank Cover Work One",
                source_url="https://example.test/low1",
                year=2025,
                raw_metadata={"page_count": 20},
            ),
        )
        u2 = catalog.upsert(
            author.id,
            "nhentai",
            DiscoveredWork(
                external_id="low-2",
                title="Completely Unrelated Work Two",
                source_url="https://example.test/low2",
                year=2025,
                raw_metadata={"page_count": 20},
            ),
        )
        aggregation._upsert_fingerprint(u1, author.name).cover_fingerprint = solid_white
        aggregation._upsert_fingerprint(u2, author.name).cover_fingerprint = solid_white
        g_u1 = await aggregation.assign(u1, author)
        g_u2 = await aggregation.assign(u2, author)
        assert g_u1.id != g_u2.id

        # Top-k fuzzy cover: higher-title-score group with mismatched cover must not shadow
        # second group with >=0.72 title score and matching cover.
        shadow = catalog.upsert(
            author.id,
            "wnacg",
            DiscoveredWork(
                external_id="tk-shadow",
                title="Summer Vacation Diary",
                source_url="https://example.test/tk1",
                year=2025,
                raw_metadata={"page_count": 24},
            ),
        )
        match = catalog.upsert(
            author.id,
            "wnacg",
            DiscoveredWork(
                external_id="tk-match",
                title="Summer Holiday Journal",
                source_url="https://example.test/tk2",
                year=2025,
                raw_metadata={"page_count": 24},
            ),
        )
        incoming = catalog.upsert(
            author.id,
            "nhentai",
            DiscoveredWork(
                external_id="tk-in",
                title="Summer Vacation Journal",
                source_url="https://example.test/tk3",
                year=2025,
                raw_metadata={"page_count": 24},
            ),
        )
        aggregation._upsert_fingerprint(shadow, author.name).cover_fingerprint = fp_a
        aggregation._upsert_fingerprint(match, author.name).cover_fingerprint = fp_b
        aggregation._upsert_fingerprint(incoming, author.name).cover_fingerprint = fp_b
        g_shadow = await aggregation.assign(shadow, author)
        g_match = await aggregation.assign(match, author)
        g_in = await aggregation.assign(incoming, author)
        assert g_in.id == g_match.id
        assert g_in.id != g_shadow.id


async def test_reconcile_matches_initial_assign_order_and_manual_split_non_shadowing() -> None:
    cover = Image.new("RGB", (320, 440), "#f3e5d8")
    ImageDraw.Draw(cover).rectangle((30, 40, 290, 400), fill="#264b77")
    fp = fingerprint_image(cover)

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        author = Author(name="OrderAuthor")
        session.add(author)
        session.flush()
        catalog = CatalogRepository(session)
        aggregation = AggregationService(session)

        # Ingest without cover first, then backfill cover + reconcile (order independence).
        w1 = catalog.upsert(
            author.id,
            "wnacg",
            DiscoveredWork(
                external_id="ord-1",
                title="Moonlit Summer Holiday",
                source_url="https://example.test/ord1",
                year=2025,
                raw_metadata={"page_count": 26},
            ),
        )
        w2 = catalog.upsert(
            author.id,
            "nhentai",
            DiscoveredWork(
                external_id="ord-2",
                title="Moonlit Summer Break",
                source_url="https://example.test/ord2",
                year=2025,
                raw_metadata={"page_count": 26},
            ),
        )
        g1 = aggregation.assign_without_cover(w1, author)
        g2 = aggregation.assign_without_cover(w2, author)
        assert g1.id != g2.id

        w1.fingerprint.cover_fingerprint = fp
        w2.fingerprint.cover_fingerprint = fp
        session.flush()
        await aggregation.reaggregate_author(author)
        assert w1.group_membership.group_id == w2.group_membership.group_id

        # Splitting a member into a manual group must not shadow another auto group target.
        w3 = catalog.upsert(
            author.id,
            "local",
            DiscoveredWork(
                external_id="ord-3",
                title="Moonlit Summer Holiday",
                source_url="https://example.test/ord3",
                year=2025,
                raw_metadata={"page_count": 26},
            ),
        )
        g_split = aggregation.split_member(w1.group_membership.group, w2.id)
        assert g_split.members[0].is_manual is True
        g3 = await aggregation.assign(w3, author)
        # w3 must be able to reconcile with non-manual w1 group even if g_split shares title.
        await aggregation.reaggregate_author(author)
        assert g3.id == w1.group_membership.group_id or w3.group_membership.group_id == (
            w1.group_membership.group_id
        )
        assert w2.group_membership.group_id == g_split.id


def test_prune_invalid_suggestions_and_backfill_batching() -> None:
    from app.modules.catalog.aggregation import backfill_work_groups, prune_invalid_suggestions

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        author = Author(name="PruneAuthor")
        session.add(author)
        session.flush()
        catalog = CatalogRepository(session)
        w1 = catalog.upsert(
            author.id,
            "wnacg",
            DiscoveredWork(
                external_id="bf-1",
                title="Backfill Comic 第1話",
                source_url="https://example.test/bf1",
            ),
        )
        w2 = catalog.upsert(
            author.id,
            "wnacg",
            DiscoveredWork(
                external_id="bf-2",
                title="Backfill Comic 第2話",
                source_url="https://example.test/bf2",
            ),
        )
        assert backfill_work_groups(session) == 2
        g1 = w1.group_membership.group
        g2 = w2.group_membership.group
        assert g1.id != g2.id

        session.add(
            MergeSuggestion(
                source_group_id=g1.id,
                target_group_id=g2.id,
                confidence=0.85,
                reasons=["legacy chapter candidate"],
            )
        )
        session.commit()

        assert prune_invalid_suggestions(session) == 1
        assert list(session.scalars(select(MergeSuggestion))) == []
