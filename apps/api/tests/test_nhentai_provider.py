import json
import zipfile
from pathlib import Path

import httpx
import pytest
from app.providers.errors import ProviderError
from app.providers.nhentai import NhentaiProvider

FIXTURES = Path(__file__).parent / "fixtures"


async def _no_sleep(_: float) -> None:
    return None


class FakeClock:
    def __init__(self) -> None:
        self.value = 1000.0
        self.slept: list[float] = []

    def now(self) -> float:
        return self.value

    async def sleep(self, seconds: float) -> None:
        self.slept.append(round(seconds, 4))
        self.value += seconds


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
        "id": 77,
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
                "tag_ids": [77, 12227],
            }
        ],
        "num_pages": 2,
    }
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if request.url.path == "/api/v2/tags/artist/mignon":
            return httpx.Response(200, json=artist_payload)
        if request.url.path == "/api/v2/galleries/tagged":
            assert request.url.params["tag_id"] == "77"
            assert request.url.params["per_page"] == "100"
            page = request.url.params.get("page")
            payload = listing_payload if page == "1" else {"result": [], "num_pages": 2}
            return httpx.Response(200, json=payload)
        if request.url.path == "/api/v2/galleries/123456":
            return httpx.Response(200, json=gallery_payload)
        if request.url.host == "i.nhentai.net":
            return httpx.Response(200, content=f"image:{request.url.path}".encode())
        return httpx.Response(404, json={"error": "Not found"})

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
    # Every metadata request goes to the JSON API; HTML pages are behind Cloudflare.
    assert not any(path.startswith(("/artist/", "/search/", "/g/")) for path in requested)
    await client.aclose()


async def test_keeps_listed_gallery_when_detail_lookup_fails() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/tags/artist/mignon":
            return httpx.Response(200, json={"id": 77, "type": "artist", "name": "mignon"})
        if request.url.path == "/api/v2/galleries/tagged":
            return httpx.Response(
                200,
                json={
                    "result": [
                        {
                            "id": 5,
                            "media_id": "50",
                            "english_title": "Listed",
                            "thumbnail": "galleries/50/thumb.jpg",
                            "num_pages": 3,
                            "tag_ids": [77],
                        }
                    ],
                    "num_pages": 1,
                },
            )
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = NhentaiProvider(user_agent="test", client=client, sleep=_no_sleep)
        works = await provider.discover_by_author("mignon")

    assert [work.title for work in works] == ["Listed"]
    assert works[0].raw_metadata["artists"] == ["mignon"]


async def test_resolves_artist_tag_from_exact_search_when_slug_is_missing() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(f"{request.url.path}?{request.url.query.decode()}")
        if request.url.path == "/api/v2/tags/artist/shin-dol":
            return httpx.Response(404, json={"error": "Tag not found"})
        if request.url.path == "/api/v2/search":
            assert request.url.params["query"] == 'artist:"Shin Dol"'
            return httpx.Response(
                200,
                json={
                    "result": [
                        {"id": 1, "media_id": "10", "english_title": "A", "tag_ids": [9, 3]},
                        {"id": 2, "media_id": "20", "english_title": "B", "tag_ids": [9]},
                    ],
                    "num_pages": 1,
                },
            )
        if request.url.path == "/api/v2/tags/ids":
            assert request.url.params["ids"] == "9,3"
            return httpx.Response(
                200,
                json=[
                    {"id": 9, "type": "artist", "name": "shindol"},
                    {"id": 3, "type": "language", "name": "japanese"},
                ],
            )
        if request.url.path == "/api/v2/galleries/tagged":
            assert request.url.params["tag_id"] == "9"
            return httpx.Response(
                200,
                json={
                    "result": [
                        {"id": 1, "media_id": "10", "english_title": "A", "tag_ids": [9]}
                    ],
                    "num_pages": 1,
                },
            )
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = NhentaiProvider(user_agent="test", client=client, sleep=_no_sleep)
        works = await provider.discover_by_author("Shin Dol")

    assert [work.external_id for work in works] == ["1"]
    assert works[0].raw_metadata["artists"] == ["shindol"]


async def test_discovers_provider_alias_from_plain_search_tags() -> None:
    requested_urls: list[str] = []

    def listing(ids: list[int], tags: dict[int, list[int]]) -> dict[str, object]:
        return {
            "result": [
                {
                    "id": gallery_id,
                    "media_id": gallery_id + 1000,
                    "english_title": f"[Ramanda] Work {gallery_id}",
                    "japanese_title": f"作品 {gallery_id}",
                    "thumbnail": f"galleries/{gallery_id + 1000}/thumb.webp",
                    "num_pages": 2,
                    "tag_ids": tags.get(gallery_id, []),
                }
                for gallery_id in ids
            ],
            "num_pages": 1,
        }

    def handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        query = request.url.params.get("query")
        if request.url.path == "/api/v2/tags/artist/ラマンダ":
            return httpx.Response(404, json={"error": "Tag not found"})
        if request.url.path == "/api/v2/search" and query == 'artist:"ラマンダ"':
            return httpx.Response(200, json=listing([], {}))
        if request.url.path == "/api/v2/search" and query == "ラマンダ":
            tags = {101: [500, 6346], 102: [500, 6346], 103: [600, 6346]}
            return httpx.Response(200, json=listing([101, 102, 103], tags))
        if request.url.path == "/api/v2/tags/ids":
            assert set(request.url.params["ids"].split(",")) == {"500", "6346"}
            return httpx.Response(
                200,
                json=[
                    {"id": 500, "type": "artist", "name": "ramanda"},
                    {"id": 6346, "type": "language", "name": "japanese"},
                ],
            )
        if request.url.path == "/api/v2/galleries/tagged":
            assert request.url.params["tag_id"] == "500"
            return httpx.Response(200, json=listing([101, 102], {101: [500], 102: [500]}))
        if request.url.path.startswith("/api/v2/galleries/"):
            return httpx.Response(503)
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = NhentaiProvider(user_agent="test", client=client, sleep=_no_sleep)

    with pytest.raises(ProviderError, match="请核对并加入作者别名"):
        await provider.discover_by_author("ラマンダ")
    works = await provider.discover_by_author("ラマンダ", author_names={"ラマンダ", "ramanda"})

    assert sorted(work.external_id for work in works) == ["101", "102"]
    assert all(work.raw_metadata["artists"] == ["ramanda"] for work in works)
    assert any("query=%E3%83%A9%E3%83%9E%E3%83%B3%E3%83%80" in url for url in requested_urls)
    # Alias inference uses one batched tag lookup instead of a detail request per gallery.
    assert not any("/api/v2/galleries/10" in url for url in requested_urls[:6])
    await client.aclose()


async def test_waits_for_retry_after_and_retries_rate_limited_request() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "7"}, json={"error": "limit"})
        return httpx.Response(200, json={"id": 1, "type": "artist", "name": "mignon"})

    clock = FakeClock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = NhentaiProvider(
            user_agent="test", client=client, sleep=clock.sleep, clock=clock.now
        )
        payload = await provider._request_api("/api/v2/tags/artist/mignon", bucket="tag")

    assert payload["name"] == "mignon"
    assert attempts == 2
    assert clock.slept == [7.0]


async def test_rate_limiter_paces_requests_to_documented_quota() -> None:
    clock = FakeClock()
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"result": [], "num_pages": 1})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = NhentaiProvider(
            user_agent="test", client=client, sleep=clock.sleep, clock=clock.now
        )
        for _ in range(10):
            await provider._request_api("/api/v2/search", bucket="search", params={"query": "x"})

    assert calls == 10
    # Anonymous search allows 10/min; the provider keeps one request in reserve.
    assert len(clock.slept) == 1
    assert clock.slept[0] == pytest.approx(60.05)


async def test_sends_optional_api_key_only_to_api_requests() -> None:
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[request.url.host] = request.headers.get("Authorization")
        if request.url.host == "nhentai.net":
            return httpx.Response(200, json={"id": 1, "type": "artist", "name": "mignon"})
        return httpx.Response(200, headers={"content-type": "image/jpeg"}, content=b"jpeg")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = NhentaiProvider(user_agent="test", api_key="secret", client=client)
        await provider._request_api("/api/v2/tags/artist/mignon", bucket="tag")
        await provider.fetch_cover("1", "https://t.nhentai.net/galleries/10/cover.jpg")

    assert seen == {"nhentai.net": "Key secret", "t.nhentai.net": None}


def test_normal_cloudflare_javascript_is_not_a_challenge_page() -> None:
    html = '<script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js"></script>'

    assert NhentaiProvider._is_challenge(html) is False


async def test_reports_cloudflare_without_bypassing_it() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(403, text="<title>Just a moment...</title>")
        )
    )
    provider = NhentaiProvider(user_agent="test", client=client)

    with pytest.raises(ProviderError, match="Cloudflare"):
        await provider.discover_by_author("mignon")
    await client.aclose()


async def test_rejects_untrusted_cover_host() -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
    provider = NhentaiProvider(user_agent="test", client=client)

    with pytest.raises(ProviderError, match="不受信任"):
        await provider.fetch_cover("123456", "https://attacker.example/cover.jpg")
    await client.aclose()


@pytest.mark.parametrize(("status", "attempts"), [(403, 1), (429, 3), (503, 1)])
async def test_artist_source_error_stops_before_search_fallback(status: int, attempts: int) -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return httpx.Response(status, text="source unavailable")

    clock = FakeClock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = NhentaiProvider(
            user_agent="test", client=client, sleep=clock.sleep, clock=clock.now
        )
        with pytest.raises(ProviderError, match=f"HTTP {status}"):
            await provider.discover_by_author("mignon")
    assert requests == ["/api/v2/tags/artist/mignon"] * attempts


async def test_artist_transport_error_stops_before_search_fallback() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        raise httpx.ConnectTimeout("timeout", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = NhentaiProvider(user_agent="test", client=client)
        with pytest.raises(ProviderError, match="超时"):
            await provider.discover_by_author("mignon")
    assert requests == ["/api/v2/tags/artist/mignon"]


def _gallery_with_pages(count: int) -> dict[str, object]:
    payload = json.loads((FIXTURES / "nhentai_gallery.json").read_text())
    payload.pop("images")
    payload["pages"] = [
        {"number": index, "path": f"galleries/3000000/{index}.jpg"}
        for index in range(1, count + 1)
    ]
    return payload


async def test_download_falls_back_to_cdn_mirrors_and_keeps_page_order(tmp_path: Path) -> None:
    attempts: dict[str, list[str]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/galleries/123456":
            return httpx.Response(200, json=_gallery_with_pages(7))
        if request.url.path == "/api/v2/cdn":
            return httpx.Response(
                200,
                json={
                    "image_servers": [
                        "https://i1.nhentai.net",
                        "https://i2.nhentai.net",
                        "https://attacker.example",
                    ]
                },
            )
        if request.url.host.endswith(".nhentai.net"):
            hosts = attempts.setdefault(request.url.path, [])
            hosts.append(request.url.host)
            if request.url.path.endswith("/2.jpg") and len(hosts) == 1:
                raise httpx.ConnectTimeout("slow", request=request)
            if request.url.path.endswith("/5.jpg") and len(hosts) < 3:
                return httpx.Response(502)
            return httpx.Response(
                200, headers={"content-type": "image/jpeg"}, content=request.url.path.encode()
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = NhentaiProvider(user_agent="test", client=client, sleep=_no_sleep)
        output = tmp_path / "book.cbz"
        await provider.download_chapter("123456", "123456", str(output))

    assert attempts["/galleries/3000000/2.jpg"] == ["i.nhentai.net", "i1.nhentai.net"]
    assert attempts["/galleries/3000000/5.jpg"] == [
        "i.nhentai.net",
        "i1.nhentai.net",
        "i2.nhentai.net",
    ]
    assert all("attacker.example" not in hosts for hosts in attempts.values())
    with zipfile.ZipFile(output) as archive:
        names = archive.namelist()
        assert names == [f"{index:04d}.jpg" for index in range(1, 8)]
        assert [archive.read(name) for name in names] == [
            f"/galleries/3000000/{index}.jpg".encode() for index in range(1, 8)
        ]


@pytest.mark.parametrize(
    ("response", "expected_requests", "message"),
    [
        (httpx.Response(403), 1, "HTTP 403"),
        (
            httpx.Response(200, headers={"content-type": "text/html"}, text="<html>x</html>"),
            4,
            "不是有效图片",
        ),
    ],
)
async def test_download_rejects_bad_pages_without_leaving_partial_file(
    tmp_path: Path, response: httpx.Response, expected_requests: int, message: str
) -> None:
    image_requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/galleries/123456":
            return httpx.Response(200, json=_gallery_with_pages(1))
        if request.url.path == "/api/v2/cdn":
            return httpx.Response(200, json={"image_servers": ["https://i1.nhentai.net"]})
        image_requests.append(str(request.url))
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = NhentaiProvider(user_agent="test", client=client, sleep=_no_sleep)
        with pytest.raises(ProviderError, match=message) as error:
            await provider.download_chapter("123456", "123456", str(tmp_path / "book.cbz"))

    assert "galleries/3000000" not in str(error.value)
    assert len(image_requests) == expected_requests
    assert list(tmp_path.iterdir()) == []
