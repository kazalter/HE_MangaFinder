import asyncio
import re
import unicodedata
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
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


@dataclass(frozen=True, slots=True)
class NhentaiGallery:
    external_id: str
    media_id: str
    title: str
    cover_url: str | None
    image_urls: list[str]
    language: str
    upload_date: datetime | None
    artists: list[str]
    tags: list[str]
    alt_titles: list[str]
    scanlator: str | None


class NhentaiProvider:
    name = "nhentai"
    display_name = "nHentai"
    capabilities = frozenset(
        {
            ProviderCapability.AUTHOR_DISCOVERY,
            ProviderCapability.CHAPTER_LIST,
            ProviderCapability.DOWNLOAD,
        }
    )

    _image_extensions = {
        "j": "jpg",
        "p": "png",
        "g": "gif",
        "w": "webp",
        "a": "avif",
    }

    def __init__(
        self,
        user_agent: str,
        base_url: str = "https://nhentai.net",
        proxy_url: str = "",
        cookie: str = "",
        max_search_pages: int = 3,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._max_search_pages = max(1, min(max_search_pages, 20))
        self._owns_client = client is None
        headers = {
            "User-Agent": user_agent,
            "Accept": "application/json,text/html;q=0.8,*/*;q=0.5",
            "Accept-Language": "en-US,en;q=0.9,ja;q=0.7",
        }
        if cookie:
            headers["Cookie"] = cookie
        self._client = client or httpx.AsyncClient(
            headers=headers,
            proxy=proxy_url or None,
            trust_env=False,
            follow_redirects=True,
            timeout=httpx.Timeout(30.0),
            limits=httpx.Limits(max_connections=5),
        )

    async def discover_by_author(self, author_name: str) -> list[DiscoveredWork]:
        normalized_author = " ".join(author_name.split()).strip()
        query = f'artist:"{normalized_author.replace(chr(34), "")}"'
        galleries: dict[str, NhentaiGallery] = {}
        total_pages = 1
        page = 1
        while page <= min(total_pages, self._max_search_pages):
            payload = await self._request_json(
                "/api/galleries/search",
                params={"query": query, "page": str(page), "sort": "recent"},
            )
            parsed, total_pages = self.parse_search(payload)
            for gallery in parsed:
                if self._has_author(gallery, normalized_author):
                    galleries[gallery.external_id] = gallery
            if page >= min(total_pages, self._max_search_pages):
                break
            page += 1
        if not galleries:
            raise AuthorNotFoundError(f'nHentai 未找到作者“{author_name}”')
        return sort_discovered_works(
            [self._discovered_work(gallery) for gallery in galleries.values()]
        )

    async def list_chapters(self, work_external_id: str) -> list[Chapter]:
        gallery = await self._gallery(work_external_id)
        return [
            Chapter(
                external_id=gallery.external_id,
                title="全本",
                number=None,
                language=gallery.language,
                published_at=gallery.upload_date,
                source_url=f"{self._base_url}/g/{gallery.external_id}/",
            )
        ]

    async def download_chapter(
        self, work_external_id: str, chapter_external_id: str, destination: str
    ) -> str:
        self._validate_id(work_external_id)
        if chapter_external_id != work_external_id:
            raise ProviderError("nHentai 章节不属于所选作品")
        gallery = await self._gallery(work_external_id)
        if not gallery.image_urls:
            raise ProviderError("nHentai 详情缺少图片列表")

        output = Path(destination)
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(output.suffix + ".part")
        referer = f"{self._base_url}/g/{gallery.external_id}/"
        try:
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for index, image_url in enumerate(gallery.image_urls, start=1):
                    try:
                        response = await self._client.get(
                            image_url, headers={"Referer": referer}
                        )
                        response.raise_for_status()
                    except httpx.HTTPError as exc:
                        raise ProviderError(f"nHentai 第 {index} 页下载失败: {exc}") from exc
                    suffix = Path(response.url.path).suffix or Path(image_url).suffix or ".jpg"
                    archive.writestr(f"{index:04d}{suffix}", response.content)
                    await asyncio.sleep(0.06)
            temporary.replace(output)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return str(output)

    async def _gallery(self, external_id: str) -> NhentaiGallery:
        self._validate_id(external_id)
        payload = await self._request_json(f"/api/gallery/{external_id}")
        return self.parse_gallery(payload)

    async def _request_json(
        self, path: str, params: dict[str, str] | None = None
    ) -> dict[str, Any]:
        try:
            response = await self._client.get(
                f"{self._base_url}{path}",
                params=params,
                headers={"Referer": f"{self._base_url}/"},
            )
            response.raise_for_status()
            if self._is_challenge(response.text):
                raise ProviderError("nHentai 触发 Cloudflare，请配置可用代理或 Cookie")
            payload = response.json()
            if not isinstance(payload, dict):
                raise ProviderError("nHentai 返回了无效 JSON")
            return payload
        except ProviderError:
            raise
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in {403, 429, 503}:
                raise ProviderError(
                    "nHentai 拒绝访问或触发 Cloudflare，请配置可用代理或 Cookie"
                ) from exc
            raise ProviderError(f"nHentai 请求失败: HTTP {exc.response.status_code}") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError(f"nHentai 请求失败: {exc}") from exc

    @classmethod
    def parse_search(
        cls, payload: dict[str, Any]
    ) -> tuple[list[NhentaiGallery], int]:
        raw_results = payload.get("result")
        if not isinstance(raw_results, list):
            return [], 1
        galleries = [
            cls.parse_gallery(item) for item in raw_results if isinstance(item, dict)
        ]
        num_pages = payload.get("num_pages", 1)
        return galleries, int(num_pages) if isinstance(num_pages, int | str) else 1

    @classmethod
    def parse_gallery(cls, payload: dict[str, Any]) -> NhentaiGallery:
        external_id = str(payload.get("id") or "")
        media_id = str(payload.get("media_id") or "")
        if not external_id.isdigit() or not media_id.isdigit():
            raise ProviderError("nHentai gallery 缺少有效 ID")
        titles = payload.get("title") if isinstance(payload.get("title"), dict) else {}
        alt_titles = [
            cls._text(str(titles.get(key) or ""))
            for key in ("english", "japanese", "pretty")
        ]
        alt_titles = list(dict.fromkeys(value for value in alt_titles if value))
        title = cls._text(str(titles.get("pretty") or titles.get("english") or ""))
        title = title or (alt_titles[0] if alt_titles else f"#{external_id}")

        raw_tags = payload.get("tags") if isinstance(payload.get("tags"), list) else []
        typed_tags = [item for item in raw_tags if isinstance(item, dict)]
        artists = cls._tag_names(typed_tags, "artist")
        languages = cls._tag_names(typed_tags, "language")
        language = cls._language(languages)
        tags = [
            cls._text(str(item.get("name") or ""))
            for item in typed_tags
            if item.get("type") in {"tag", "category", "parody", "group"}
        ]

        images = payload.get("images") if isinstance(payload.get("images"), dict) else {}
        raw_pages = images.get("pages") if isinstance(images.get("pages"), list) else []
        image_urls = [
            f"https://i.nhentai.net/galleries/{media_id}/{index}.{cls._extension(page)}"
            for index, page in enumerate(raw_pages, start=1)
            if isinstance(page, dict)
        ]
        cover = images.get("cover") if isinstance(images.get("cover"), dict) else None
        cover_url = (
            f"https://t.nhentai.net/galleries/{media_id}/cover.{cls._extension(cover)}"
            if cover
            else None
        )
        upload_timestamp = payload.get("upload_date")
        upload_date = (
            datetime.fromtimestamp(upload_timestamp, UTC)
            if isinstance(upload_timestamp, int | float)
            else None
        )
        scanlator = cls._text(str(payload.get("scanlator") or "")) or None
        return NhentaiGallery(
            external_id=external_id,
            media_id=media_id,
            title=title,
            cover_url=cover_url,
            image_urls=image_urls,
            language=language,
            upload_date=upload_date,
            artists=artists,
            tags=list(dict.fromkeys(tag for tag in tags if tag)),
            alt_titles=alt_titles,
            scanlator=scanlator,
        )

    def _discovered_work(self, gallery: NhentaiGallery) -> DiscoveredWork:
        return DiscoveredWork(
            external_id=gallery.external_id,
            title=gallery.title,
            source_url=f"{self._base_url}/g/{gallery.external_id}/",
            cover_url=gallery.cover_url,
            status="completed",
            year=None,
            language=gallery.language,
            tags=gallery.tags,
            source_updated_at=gallery.upload_date,
            raw_metadata={
                "altTitles": gallery.alt_titles,
                "page_count": len(gallery.image_urls),
                "artists": gallery.artists,
                "media_id": gallery.media_id,
                "scanlator": gallery.scanlator,
                "uploaded_at": (
                    gallery.upload_date.isoformat() if gallery.upload_date else None
                ),
            },
        )

    @classmethod
    def _extension(cls, image: dict[str, Any]) -> str:
        image_type = str(image.get("t") or "")
        extension = cls._image_extensions.get(image_type)
        if extension is None:
            raise ProviderError(f"nHentai 不支持图片类型: {image_type or 'unknown'}")
        return extension

    @staticmethod
    def _tag_names(tags: list[dict[str, Any]], tag_type: str) -> list[str]:
        return [
            NhentaiProvider._text(str(item.get("name") or ""))
            for item in tags
            if item.get("type") == tag_type and item.get("name")
        ]

    @staticmethod
    def _language(languages: list[str]) -> str:
        values = {value.casefold() for value in languages}
        if "chinese" in values:
            return "zh-Hans"
        if "japanese" in values:
            return "ja"
        if "english" in values:
            return "en"
        return "und"

    @staticmethod
    def _has_author(gallery: NhentaiGallery, author_name: str) -> bool:
        expected = NhentaiProvider._identity(author_name)
        return any(NhentaiProvider._identity(value) == expected for value in gallery.artists)

    @staticmethod
    def _identity(value: str) -> str:
        normalized = unicodedata.normalize("NFKC", value).casefold()
        return re.sub(r"[^\w\u3040-\u30ff\u3400-\u9fff]+", "", normalized)

    @staticmethod
    def _text(value: str) -> str:
        return " ".join(value.split()).strip()

    @staticmethod
    def _is_challenge(value: str) -> bool:
        lowered = value.casefold()
        return any(
            marker in lowered
            for marker in (
                "cf-mitigated",
                "challenge-platform",
                "just a moment...",
                "attention required! | cloudflare",
            )
        )

    @staticmethod
    def _validate_id(value: str) -> None:
        if not value.isdigit():
            raise ProviderError("nHentai gallery ID 无效")

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
