import json
import zipfile
from pathlib import Path

import httpx

from app.providers.mangadex import MangaDexProvider

FIXTURES = Path(__file__).parent / "fixtures"


async def test_discovers_and_normalizes_author_works() -> None:
    author_payload = json.loads((FIXTURES / "mangadex_author.json").read_text())
    manga_payload = json.loads((FIXTURES / "mangadex_manga.json").read_text())

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/author":
            return httpx.Response(200, json=author_payload)
        if request.url.path == "/manga":
            assert request.url.params.get("authors[]") == "author-1"
            return httpx.Response(200, json=manga_payload)
        return httpx.Response(404)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.mangadex.test"
    )
    provider = MangaDexProvider(user_agent="test", client=client)

    works = await provider.discover_by_author("Asano Inio")

    assert len(works) == 1
    assert works[0].title == "Goodnight Punpun"
    assert works[0].status == "completed"
    assert works[0].tags == ["Drama"]
    assert works[0].cover_url == (
        "https://uploads.mangadex.org/covers/manga-1/cover.jpg.512.jpg"
    )
    await client.aclose()


async def test_download_validates_work_and_creates_cbz(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/chapter/chapter-1":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "relationships": [{"id": "manga-1", "type": "manga"}]
                    }
                },
            )
        if request.url.path == "/at-home/server/chapter-1":
            return httpx.Response(
                200,
                json={
                    "baseUrl": "https://uploads.test",
                    "chapter": {
                        "hash": "hash-1",
                        "data": ["page.png"],
                        "dataSaver": ["page.jpg"],
                    },
                },
            )
        if request.url.path == "/data-saver/hash-1/page.jpg":
            return httpx.Response(200, content=b"image-bytes")
        return httpx.Response(404)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.mangadex.test"
    )
    provider = MangaDexProvider(user_agent="test", client=client, use_data_saver=True)
    output = tmp_path / "chapter.cbz"

    result = await provider.download_chapter("manga-1", "chapter-1", str(output))

    assert result == str(output)
    with zipfile.ZipFile(output) as archive:
        assert archive.namelist() == ["0001.jpg"]
        assert archive.read("0001.jpg") == b"image-bytes"
    await client.aclose()
