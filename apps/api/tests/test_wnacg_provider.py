import zipfile
from pathlib import Path

import httpx
import pytest

from app.providers.errors import ProviderError
from app.providers.wnacg import WnacgProvider

FIXTURES = Path(__file__).parent / "fixtures"


def test_parses_search_results_and_pagination() -> None:
    html = (FIXTURES / "wnacg_search.html").read_text()

    works, pages = WnacgProvider.parse_search(html, "https://mirror.example.test")

    assert pages == 2
    assert len(works) == 1
    assert works[0].external_id == "277305"
    assert works[0].title == "[示例作者] 測試作品 [完結]"
    assert works[0].status == "completed"
    assert works[0].year is None
    assert works[0].source_updated_at.year == 2024
    assert works[0].raw_metadata["uploaded_at"] == "2024-12-05T00:00:00+00:00"
    assert works[0].tags == ["韓漫 / 漢化"]
    assert works[0].raw_metadata["page_count"] == 16
    assert works[0].cover_url == "https://t4.example.test/data/t/2773/05/cover.jpg"


async def test_falls_back_to_mirror_and_discovers() -> None:
    search_html = (FIXTURES / "wnacg_search.html").read_text()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.wnacg.com":
            return httpx.Response(403, text="Just a moment...")
        if request.url.host == "mirror.example.test":
            return httpx.Response(200, text=search_html)
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = WnacgProvider(
        user_agent="test",
        base_urls=["https://www.wnacg.com", "https://mirror.example.test"],
        max_search_pages=1,
        client=client,
    )

    works = await provider.discover_by_author("示例作者")

    assert [work.external_id for work in works] == ["277305"]
    assert works[0].source_url == "https://www.wnacg.com/photos-index-aid-277305.html"
    assert works[0].raw_metadata["author_match"] == "creator_credit_exact"
    await client.aclose()


def test_only_accepts_exact_leading_creator_credit() -> None:
    assert WnacgProvider.matches_creator_credit(
        "(C106) [YD WORKS (YD)] 正确作品 [白杨汉化组]", "YD"
    )
    assert WnacgProvider.matches_creator_credit(
        "[实际作者] 正确作品 [YD汉化]", "实际作者"
    )
    assert not WnacgProvider.matches_creator_credit(
        "[实际作者] 正确作品 [YD汉化]", "YD"
    )
    assert not WnacgProvider.matches_creator_credit(
        "[YD汉化] 只有发布者命中", "YD汉化"
    )
    assert not WnacgProvider.matches_creator_credit(
        "标题正文提到了 YD", "YD"
    )


async def test_downloads_gallery_to_cbz_and_filters_promo(tmp_path: Path) -> None:
    gallery_html = (FIXTURES / "wnacg_gallery.html").read_text()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/photos-gallery-aid-277305.html":
            return httpx.Response(200, text=gallery_html)
        if request.url.host == "img.example.test":
            return httpx.Response(200, content=f"image:{request.url.path}".encode())
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = WnacgProvider(
        user_agent="test", base_urls=["https://mirror.example.test"], client=client
    )
    output = tmp_path / "gallery.cbz"

    result = await provider.download_chapter("277305", "277305", str(output))

    assert result == str(output)
    with zipfile.ZipFile(output) as archive:
        assert archive.namelist() == ["0001.jpg", "0002.png"]
        assert archive.read("0001.jpg").startswith(b"image:")
    await client.aclose()


@pytest.mark.parametrize(
    "cover_url",
    [
        "https://t4.qy0.ru/data/t/1455/55/1647052179708.gif",
        "https://t3.wnacgimg.date/data/t/1332/29/cover.jpg",
        "https://mirror.example.test/data/t/123/cover.jpg",
    ],
)
async def test_fetches_cover_from_trusted_domains(cover_url: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"fake-image-bytes",
            headers={"content-type": "image/jpeg"},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = WnacgProvider(
        user_agent="test",
        base_urls=["https://www.wnacg.com", "https://mirror.example.test"],
        client=client,
    )

    image = await provider.fetch_cover("145555", cover_url)
    assert image.content == b"fake-image-bytes"
    assert image.content_type == "image/jpeg"
    await client.aclose()


@pytest.mark.parametrize(
    "cover_url",
    [
        "https://attacker.example/cover.jpg",
        "http://t4.qy0.ru/data/t/cover.jpg",  # insecure http
        "https://evil-qy0.ru/cover.jpg",
    ],
)
async def test_rejects_untrusted_wnacg_cover(cover_url: str) -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
    provider = WnacgProvider(
        user_agent="test",
        base_urls=["https://www.wnacg.com"],
        client=client,
    )

    with pytest.raises(ProviderError, match="不受信任"):
        await provider.fetch_cover("145555", cover_url)
    await client.aclose()


def test_matches_creator_credit_skips_scanlator_prefix_and_supports_alias_set() -> None:
    title = "[無毒汉化组](C97) [ピアニッシモ(ピジャ)] ずっと信じてる"
    assert WnacgProvider.matches_creator_credit(title, "ピジャ")
    assert WnacgProvider.matches_creator_credit(title, "ピアニッシモ")
    assert WnacgProvider.matches_creator_credit(title, "pija", {"pija", "ピジャ", "ピアニッシモ"})
    assert not WnacgProvider.matches_creator_credit(title, "無毒汉化组")
