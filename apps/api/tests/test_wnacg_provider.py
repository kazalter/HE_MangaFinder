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


async def _no_sleep(_: float) -> None:
    return None


@pytest.mark.parametrize(
    ("title", "author"),
    [
        # Real titles: Japanese comma between names, group bracket before the event.
        ("[DA HOOTCH (新堂エル、ShindoL)] 女ゆうしゃノ旅1-6 [中国翻訳]", "ShindoL"),
        ("[DA HOOTCH (新堂エル、ShindoL)] 女ゆうしゃノ旅1-6 [中国翻訳]", "新堂エル"),
        ("[祐希堂](C84) [Da Hootch (ShindoL)] TSF Monogatari Append 1.0[祐希堂]", "ShindoL"),
        ("[作者A×作者B] 合作本", "作者B"),
        ("(COMIC1☆13) [DA HOOTCH (新堂エル)] BOUDICA (FateGrand Order)", "新堂エル"),
        ("(C85) (同人誌) [DA HOOTCH (新堂エル)] PIG (ゼルダの伝説)", "新堂エル"),
        ("[有毒気漢化組] (COMIC1☆11) [Da Hootch (新堂エル)] MILDRED 準備号", "新堂エル"),
    ],
)
def test_matches_real_world_creator_credit_formats(title: str, author: str) -> None:
    assert WnacgProvider.matches_creator_credit(title, author)


def test_group_prefix_before_event_is_not_a_creator_credit() -> None:
    title = "[祐希堂](C84) [Da Hootch (ShindoL)] TSF Monogatari Append 1.0[祐希堂]"
    assert not WnacgProvider.matches_creator_credit(title, "祐希堂")


def test_reads_edition_language_from_category() -> None:
    html = (FIXTURES / "wnacg_search.html").read_text()
    for css_class, language in [("cate-20", "zh-Hant"), ("cate-13", "ja"), ("cate-17", "en")]:
        works, _ = WnacgProvider.parse_search(
            html.replace("cate-20", css_class), "https://mirror.example.test"
        )
        assert works[0].language == language


def test_normal_cloudflare_javascript_is_not_a_challenge_page() -> None:
    html = (
        "<title>搜索</title><li class='gallary_item'></li>"
        '<script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js"></script>'
    )
    assert WnacgProvider._is_challenge(html) is False
    assert WnacgProvider._is_challenge("<title>Just a moment...</title>") is True


async def test_retries_transient_image_failures(tmp_path: Path) -> None:
    gallery_html = (FIXTURES / "wnacg_gallery.html").read_text()
    attempts: dict[str, int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/photos-gallery-aid-277305.html":
            return httpx.Response(200, text=gallery_html)
        if request.url.host == "img.example.test":
            attempts[request.url.path] = attempts.get(request.url.path, 0) + 1
            if attempts[request.url.path] == 1:
                raise httpx.ReadTimeout("slow", request=request)
            if attempts[request.url.path] == 2:
                return httpx.Response(502)
            return httpx.Response(200, content=b"image")
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = WnacgProvider(
            user_agent="test",
            base_urls=["https://mirror.example.test"],
            client=client,
            sleep=_no_sleep,
        )
        await provider.download_chapter("277305", "277305", str(tmp_path / "g.cbz"))

    assert set(attempts.values()) == {3}
    with zipfile.ZipFile(tmp_path / "g.cbz") as archive:
        assert len(archive.namelist()) == 2


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(404),
        httpx.Response(200, headers={"content-type": "text/html"}, text="<html>error</html>"),
    ],
)
async def test_rejects_missing_or_html_image_without_leaving_partial_file(
    tmp_path: Path, response: httpx.Response
) -> None:
    gallery_html = (FIXTURES / "wnacg_gallery.html").read_text()
    image_requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal image_requests
        if request.url.path == "/photos-gallery-aid-277305.html":
            return httpx.Response(200, text=gallery_html)
        image_requests += 1
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = WnacgProvider(
            user_agent="test",
            base_urls=["https://mirror.example.test"],
            client=client,
            sleep=_no_sleep,
        )
        with pytest.raises(ProviderError, match="第 1 页"):
            await provider.download_chapter("277305", "277305", str(tmp_path / "g.cbz"))

    assert image_requests == 1
    assert list(tmp_path.iterdir()) == []
