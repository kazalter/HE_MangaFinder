import zipfile
from pathlib import Path

import httpx

from app.providers.hanimeone import HanimeOneProvider

FIXTURES = Path(__file__).parent / "fixtures"


def test_parses_author_and_detail_pages() -> None:
    author_html = (FIXTURES / "hanimeone_author.html").read_text()
    detail_html = (FIXTURES / "hanimeone_detail.html").read_text()

    works = HanimeOneProvider.parse_author_page(author_html, "https://hanimeone.me")
    detail = HanimeOneProvider.parse_detail(
        detail_html, "https://hanimeone.me", "134347"
    )

    assert [work.external_id for work in works] == ["134347", "104714"]
    assert works[0].cover_url == "https://t2.nhentai.net/galleries/3000000/cover.webp"
    assert detail.title == "[MIGNON WORKS (mignon)] ONAKA SUMMER 2 [無修正]"
    assert detail.page_count == 2
    assert detail.language == "zh-Hant"
    assert detail.tags == ["無修正"]
    assert detail.image_urls == {
        1: "https://i2.nhentai.net/galleries/3000000/1.webp",
        2: "https://i2.nhentai.net/galleries/3000000/2.jpg",
    }


async def test_discovers_and_downloads_full_book(tmp_path: Path) -> None:
    author_html = (FIXTURES / "hanimeone_author.html").read_text()
    detail_html = (FIXTURES / "hanimeone_detail.html").read_text()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/artists/mignon":
            return httpx.Response(200, text=author_html)
        if request.url.path == "/comic/134347":
            return httpx.Response(200, text=detail_html)
        if request.url.host.startswith("i2.nhentai.net"):
            return httpx.Response(200, content=f"image:{request.url.path}".encode())
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = HanimeOneProvider(user_agent="test", client=client)

    works = await provider.discover_by_author("mignon")
    output = tmp_path / "hanimeone.cbz"
    result = await provider.download_chapter("134347", "134347", str(output))

    assert len(works) == 2
    assert result == str(output)
    with zipfile.ZipFile(output) as archive:
        assert archive.namelist() == ["0001.webp", "0002.jpg"]
        assert archive.read("0001.webp").startswith(b"image:")
    await client.aclose()
