import json
import zipfile
from pathlib import Path

import httpx
import pytest

from app.providers.errors import ProviderError
from app.providers.nhentai import NhentaiProvider

FIXTURES = Path(__file__).parent / "fixtures"


def embedded_html(*payloads: dict[str, object]) -> str:
    scripts = "".join(
        '<script type="application/json">'
        + json.dumps(
            {
                "status": 200,
                "statusText": "OK",
                "headers": {"content-type": "application/json"},
                "body": json.dumps(payload),
            }
        )
        + "</script>"
        for payload in payloads
    )
    return f"<!doctype html><html><body>{scripts}</body></html>"


def test_parses_search_gallery_metadata_and_images() -> None:
    payload = json.loads((FIXTURES / "nhentai_search.json").read_text())

    galleries, pages = NhentaiProvider.parse_search(payload)
    gallery = galleries[0]

    assert pages == 2
    assert gallery.external_id == "123456"
    assert gallery.title == "ONAKA SUMMER 2"
    assert gallery.cover_url == "https://t.nhentai.net/galleries/3000000/cover.webp"
    assert gallery.image_urls == [
        "https://i.nhentai.net/galleries/3000000/1.webp",
        "https://i.nhentai.net/galleries/3000000/2.jpg",
    ]
    assert gallery.language == "en"
    assert gallery.artists == ["mignon"]
    assert gallery.tags == ["full color", "doujinshi"]
    assert gallery.upload_date.isoformat() == "2026-07-12T00:00:00+00:00"


async def test_discovers_exact_artist_across_pages_and_downloads_cbz(
    tmp_path: Path,
) -> None:
    gallery_payload = json.loads((FIXTURES / "nhentai_gallery.json").read_text())
    gallery_payload["cover"] = {
        "path": "galleries/3000000/cover.webp",
        "width": 350,
        "height": 500,
    }
    gallery_payload["pages"] = [
        {"number": 1, "path": "galleries/3000000/1.webp"},
        {"number": 2, "path": "galleries/3000000/2.jpg"},
    ]
    gallery_payload.pop("images")
    artist_payload = {
        "id": 1,
        "type": "artist",
        "name": "mignon",
        "slug": "mignon",
    }
    listing_payload = {
        "result": [
            {
                "id": 123456,
                "media_id": "3000000",
                "english_title": "[MIGNON WORKS (mignon)] ONAKA SUMMER 2 [English]",
                "japanese_title": "[MIGNON WORKS (mignon)] おなかサマー2",
                "thumbnail": "galleries/3000000/thumb.webp",
                "num_pages": 2,
            }
        ],
        "num_pages": 2,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/artist/mignon/":
            page = request.url.params.get("page")
            payload = listing_payload if page == "1" else {"result": [], "num_pages": 2}
            return httpx.Response(200, text=embedded_html(artist_payload, payload))
        if request.url.path == "/g/123456/":
            return httpx.Response(200, text=embedded_html(gallery_payload))
        if request.url.host == "i.nhentai.net":
            return httpx.Response(200, content=f"image:{request.url.path}".encode())
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = NhentaiProvider(
        user_agent="test", max_search_pages=2, client=client
    )

    works = await provider.discover_by_author("MIGNON")
    output = tmp_path / "nhentai.cbz"
    result = await provider.download_chapter("123456", "123456", str(output))

    assert [work.external_id for work in works] == ["123456"]
    assert works[0].year is None
    assert works[0].raw_metadata["page_count"] == 2
    assert works[0].raw_metadata["altTitles"] == [
        "[MIGNON WORKS (mignon)] ONAKA SUMMER 2 [English]",
        "[MIGNON WORKS (mignon)] おなかサマー2",
        "ONAKA SUMMER 2",
    ]
    assert result == str(output)
    with zipfile.ZipFile(output) as archive:
        assert archive.namelist() == ["0001.webp", "0002.jpg"]
        assert archive.read("0001.webp").startswith(b"image:")
    await client.aclose()


async def test_reports_cloudflare_without_bypassing_it() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(403, text="Just a moment...")
        )
    )
    provider = NhentaiProvider(user_agent="test", client=client)

    with pytest.raises(ProviderError, match="Cloudflare"):
        await provider.discover_by_author("mignon")
    await client.aclose()
