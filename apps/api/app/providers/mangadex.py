import asyncio
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from app.providers.base import (
    Chapter,
    DiscoveredWork,
    ProviderCapability,
    sort_discovered_works,
)
from app.providers.errors import AuthorNotFoundError, ProviderError


class MangaDexProvider:
    name = "mangadex"
    display_name = "MangaDex"
    capabilities = frozenset(
        {
            ProviderCapability.AUTHOR_DISCOVERY,
            ProviderCapability.CHAPTER_LIST,
            ProviderCapability.DOWNLOAD,
        }
    )

    def __init__(
        self,
        user_agent: str,
        client: httpx.AsyncClient | None = None,
        base_url: str = "https://api.mangadex.org",
        use_data_saver: bool = True,
        chapter_languages: list[str] | None = None,
    ) -> None:
        self._owns_client = client is None
        self._use_data_saver = use_data_saver
        self._chapter_languages = chapter_languages or ["en"]
        self._client = client or httpx.AsyncClient(
            base_url=base_url,
            headers={"User-Agent": user_agent, "Accept": "application/json"},
            timeout=httpx.Timeout(20.0),
            limits=httpx.Limits(max_connections=5),
        )

    async def discover_by_author(self, author_name: str) -> list[DiscoveredWork]:
        author = await self._find_author(author_name)
        author_id = author["id"]
        results: list[DiscoveredWork] = []
        offset = 0

        while True:
            response = await self._request(
                "/manga",
                params=[
                    ("authors[]", author_id),
                    ("includes[]", "cover_art"),
                    ("includes[]", "author"),
                    ("limit", "100"),
                    ("offset", str(offset)),
                    ("order[updatedAt]", "desc"),
                    ("contentRating[]", "safe"),
                    ("contentRating[]", "suggestive"),
                    ("contentRating[]", "erotica"),
                ],
            )
            payload = response.json()
            data = payload.get("data", [])
            results.extend(self._map_work(item) for item in data)
            offset += len(data)
            if not data or offset >= payload.get("total", 0):
                break

        return sort_discovered_works(results)

    async def list_chapters(self, work_external_id: str) -> list[Chapter]:
        results: list[Chapter] = []
        offset = 0
        while True:
            params = [
                ("manga", work_external_id),
                ("limit", "100"),
                ("offset", str(offset)),
                ("order[publishAt]", "desc"),
            ]
            params.extend(
                ("translatedLanguage[]", language)
                for language in self._chapter_languages
            )
            response = await self._request("/chapter", params=params)
            payload = response.json()
            data = payload.get("data", [])
            for item in data:
                attributes = item["attributes"]
                published_at = attributes.get("publishAt")
                results.append(
                    Chapter(
                        external_id=item["id"],
                        title=attributes.get("title"),
                        number=attributes.get("chapter"),
                        language=attributes.get("translatedLanguage", "und"),
                        published_at=(
                            datetime.fromisoformat(published_at) if published_at else None
                        ),
                        source_url=f"https://mangadex.org/chapter/{item['id']}",
                    )
                )
            offset += len(data)
            if not data or offset >= payload.get("total", 0):
                break
        return results

    async def download_chapter(
        self, work_external_id: str, chapter_external_id: str, destination: str
    ) -> str:
        chapter_response = await self._request(
            f"/chapter/{chapter_external_id}", params={"includes[]": "manga"}
        )
        relationships = chapter_response.json().get("data", {}).get("relationships", [])
        belongs_to_work = any(
            relation.get("type") == "manga" and relation.get("id") == work_external_id
            for relation in relationships
        )
        if not belongs_to_work:
            raise ProviderError("章节不属于所选作品")

        server_response = await self._request(
            f"/at-home/server/{chapter_external_id}", params={}
        )
        server = server_response.json()
        chapter = server["chapter"]
        folder = "data-saver" if self._use_data_saver else "data"
        filenames = chapter["dataSaver"] if self._use_data_saver else chapter["data"]
        base_url = f"{server['baseUrl']}/{folder}/{chapter['hash']}"

        output = Path(destination)
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(output.suffix + ".part")
        try:
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for index, filename in enumerate(filenames, start=1):
                    try:
                        response = await self._client.get(f"{base_url}/{filename}")
                        response.raise_for_status()
                    except httpx.HTTPError as exc:
                        raise ProviderError(f"第 {index} 页下载失败: {exc}") from exc
                    extension = Path(filename).suffix or ".jpg"
                    archive.writestr(f"{index:04d}{extension}", response.content)
                    await asyncio.sleep(0.05)
            temporary.replace(output)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return str(output)

    async def _find_author(self, name: str) -> dict[str, Any]:
        response = await self._request("/author", params={"name": name, "limit": "20"})
        candidates = response.json().get("data", [])
        if not candidates:
            raise AuthorNotFoundError(f'MangaDex 未找到作者“{name}”')

        normalized = name.casefold().replace(" ", "")
        return next(
            (
                item
                for item in candidates
                if item.get("attributes", {}).get("name", "").casefold().replace(" ", "")
                == normalized
            ),
            candidates[0],
        )

    async def _request(self, path: str, params: Any) -> httpx.Response:
        try:
            response = await self._client.get(path, params=params)
            response.raise_for_status()
            return response
        except httpx.HTTPError as exc:
            raise ProviderError(f"MangaDex 请求失败: {exc}") from exc

    @staticmethod
    def _map_work(item: dict[str, Any]) -> DiscoveredWork:
        attributes = item["attributes"]
        manga_id = item["id"]
        title = MangaDexProvider._localized(attributes.get("title", {})) or "未命名作品"
        description = MangaDexProvider._localized(attributes.get("description", {}))
        cover_filename = next(
            (
                relation.get("attributes", {}).get("fileName")
                for relation in item.get("relationships", [])
                if relation.get("type") == "cover_art"
            ),
            None,
        )
        cover_url = (
            f"https://uploads.mangadex.org/covers/{manga_id}/{cover_filename}.512.jpg"
            if cover_filename
            else None
        )
        updated_at = attributes.get("updatedAt")
        tags = [
            MangaDexProvider._localized(tag.get("attributes", {}).get("name", {}))
            for tag in attributes.get("tags", [])
        ]

        return DiscoveredWork(
            external_id=manga_id,
            title=title,
            source_url=f"https://mangadex.org/title/{manga_id}",
            description=description,
            cover_url=cover_url,
            status=attributes.get("status"),
            year=attributes.get("year"),
            language=attributes.get("originalLanguage"),
            tags=[tag for tag in tags if tag],
            source_updated_at=datetime.fromisoformat(updated_at) if updated_at else None,
            raw_metadata={
                "altTitles": attributes.get("altTitles", []),
                "lastVolume": attributes.get("lastVolume"),
                "lastChapter": attributes.get("lastChapter"),
                "publicationDemographic": attributes.get("publicationDemographic"),
            },
        )

    @staticmethod
    def _localized(values: dict[str, str]) -> str | None:
        for language in ("zh-hans", "zh", "en", "ja-ro", "ja"):
            if values.get(language):
                return values[language]
        return next(iter(values.values()), None)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
