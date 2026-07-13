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


async def test_discovers_provider_alias_from_plain_search_details() -> None:
    base_gallery = json.loads((FIXTURES / "nhentai_gallery.json").read_text())
    requested_urls: list[str] = []

    def listing(ids: list[int]) -> dict[str, object]:
        return {
            "result": [
                {
                    "id": gallery_id,
                    "media_id": gallery_id + 1000,
                    "english_title": f"[Ramanda] Work {gallery_id}",
                    "japanese_title": f"作品 {gallery_id}",
                    "thumbnail": f"galleries/{gallery_id + 1000}/thumb.webp",
                    "num_pages": 2,
                }
                for gallery_id in ids
            ],
            "num_pages": 1,
        }

    def gallery(gallery_id: int, artist: str) -> dict[str, object]:
        payload = json.loads(json.dumps(base_gallery))
        payload["id"] = gallery_id
        payload["media_id"] = str(gallery_id + 1000)
        payload["title"]["pretty"] = f"Work {gallery_id}"
        payload["tags"][0]["name"] = artist
        return payload

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        query = request.url.params.get("q")
        if request.url.path == "/search/" and query == "artist:ラマンダ":
            return httpx.Response(200, text=embedded_html(listing([])))
        if request.url.path == "/search/" and query == "ラマンダ":
            return httpx.Response(200, text=embedded_html(listing([101, 102, 103])))
        if request.url.path == "/artist/ramanda/":
            artist = {"id": 1, "type": "artist", "name": "ramanda"}
            return httpx.Response(200, text=embedded_html(artist, listing([101, 102])))
        if request.url.path == "/g/101/":
            return httpx.Response(200, text=embedded_html(gallery(101, "ramanda")))
        if request.url.path == "/g/102/":
            return httpx.Response(200, text=embedded_html(gallery(102, "ramanda")))
        if request.url.path == "/g/103/":
            return httpx.Response(200, text=embedded_html(gallery(103, "other")))
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = NhentaiProvider(user_agent="test", client=client)

    works = await provider.discover_by_author("ラマンダ")

    assert [work.external_id for work in works] == ["101", "102"]
    assert all(work.raw_metadata["artists"] == ["ramanda"] for work in works)
    assert any("q=%E3%83%A9%E3%83%9E%E3%83%B3%E3%83%80" in url for url in requested_urls)
    assert any("/artist/ramanda/" in url for url in requested_urls)
    await client.aclose()


def test_normal_cloudflare_javascript_is_not_a_challenge_page() -> None:
    html = '<script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js"></script>'

    assert NhentaiProvider._is_challenge(html) is False


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
