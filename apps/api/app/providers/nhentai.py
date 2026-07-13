import asyncio
import json
import re
import unicodedata
import zipfile
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup

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
    page_count: int


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
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.8,*/*;q=0.5",
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
        path, query_author, first_response = await self._find_author_listing(
            normalized_author
        )
        parsed, total_pages = self.parse_listing(first_response.text, query_author)
        galleries: dict[str, NhentaiGallery] = {}
        for gallery in parsed:
            galleries[gallery.external_id] = gallery

        for page in range(2, min(total_pages, self._max_search_pages) + 1):
            params = {"sort": "date", "page": str(page)}
            if path == "/search/":
                params["q"] = f"artist:{query_author}"
            response = await self._request_page(path, params=params)
            parsed, _ = self.parse_listing(response.text, query_author)
            for gallery in parsed:
                galleries[gallery.external_id] = gallery

        if not galleries:
            raise AuthorNotFoundError(f'nHentai 未找到作者“{author_name}”')

        semaphore = asyncio.Semaphore(2)

        async def enrich(gallery: NhentaiGallery) -> NhentaiGallery | None:
            async with semaphore:
                for attempt in range(2):
                    try:
                        detailed = await self._gallery(gallery.external_id)
                        break
                    except ProviderError:
                        if attempt == 1:
                            return gallery
                        await asyncio.sleep(0.35)
                await asyncio.sleep(0.08)
            if self._has_author(detailed, query_author):
                return detailed
            return None

        listed = list(galleries.values())
        detail_limit = min(8, len(listed))
        enriched = await asyncio.gather(*(enrich(item) for item in listed[:detail_limit]))
        exact = [item for item in enriched if item is not None]
        exact.extend(listed[detail_limit:])
        if not exact:
            raise AuthorNotFoundError(f'nHentai 未找到作者“{author_name}”')
        return sort_discovered_works(
            [self._discovered_work(gallery) for gallery in exact]
        )

    async def _find_author_listing(
        self, author_name: str
    ) -> tuple[str, str, httpx.Response]:
        """Find an authoritative listing, resolving site-specific aliases safely."""

        path = f"/artist/{quote(self._slug(author_name), safe='')}/"
        try:
            response = await self._request_page(
                path, params={"sort": "date", "page": "1"}
            )
            listed_artist = self.parse_artist(response.text)
            if listed_artist and self._identity(listed_artist) == self._identity(author_name):
                return path, listed_artist, response
        except ProviderError:
            pass

        exact_response = await self._request_page(
            "/search/",
            params={
                "q": f"artist:{author_name}",
                "sort": "date",
                "page": "1",
            },
        )
        exact_galleries, _ = self.parse_listing(exact_response.text, author_name)
        if exact_galleries:
            return "/search/", author_name, exact_response

        alias = await self._discover_author_alias(author_name)
        if alias is None:
            raise AuthorNotFoundError(f'nHentai 未找到作者“{author_name}”')

        alias_path = f"/artist/{quote(self._slug(alias), safe='')}/"
        alias_response = await self._request_page(
            alias_path, params={"sort": "date", "page": "1"}
        )
        listed_alias = self.parse_artist(alias_response.text)
        if not listed_alias or self._identity(listed_alias) != self._identity(alias):
            raise AuthorNotFoundError(f'nHentai 未找到作者“{author_name}”')
        return alias_path, listed_alias, alias_response

    async def _discover_author_alias(self, author_name: str) -> str | None:
        """Infer one dominant real artist tag from a bounded plain-search sample."""

        response = await self._request_page(
            "/search/",
            params={"q": author_name, "sort": "date", "page": "1"},
        )
        candidates, _ = self.parse_listing(response.text, author_name)
        sample = candidates[:8]
        if len(sample) < 2:
            return None

        semaphore = asyncio.Semaphore(2)

        async def detail(gallery: NhentaiGallery) -> NhentaiGallery | None:
            async with semaphore:
                try:
                    result = await self._gallery(gallery.external_id)
                except ProviderError:
                    return None
                await asyncio.sleep(0.08)
                return result

        resolved = await asyncio.gather(*(detail(item) for item in sample))
        details = [item for item in resolved if item]
        if len(details) < 2:
            return None

        counts: Counter[str] = Counter()
        display_names: dict[str, str] = {}
        for gallery in details:
            for artist in set(gallery.artists):
                identity = self._identity(artist)
                if identity:
                    counts[identity] += 1
                    display_names.setdefault(identity, artist)
        if not counts:
            return None

        ranked = counts.most_common(2)
        identity, support = ranked[0]
        tied = len(ranked) > 1 and ranked[1][1] == support
        if support < 2 or support / len(details) < 0.6 or tied:
            return None
        return display_names[identity]

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
        response = await self._request_page(f"/g/{external_id}/")
        payload = self.extract_gallery_payload(response.text)
        if payload is None:
            raise ProviderError("nHentai 详情页缺少作品数据，页面结构可能已变化")
        return self.parse_gallery(payload)

    async def _request_page(
        self, path: str, params: dict[str, str] | None = None
    ) -> httpx.Response:
        try:
            response = await self._client.get(
                f"{self._base_url}{path}",
                params=params,
                headers={"Referer": f"{self._base_url}/"},
            )
            response.raise_for_status()
            if self._is_challenge(response.text):
                raise ProviderError("nHentai 触发 Cloudflare，请配置可用代理或 Cookie")
            return response
        except ProviderError:
            raise
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in {403, 429, 503}:
                raise ProviderError(
                    "nHentai 拒绝访问或触发 Cloudflare，请配置可用代理或 Cookie"
                ) from exc
            raise ProviderError(f"nHentai 请求失败: HTTP {exc.response.status_code}") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"nHentai 请求失败: {exc}") from exc

    @classmethod
    def parse_listing(
        cls, html: str, assumed_author: str
    ) -> tuple[list[NhentaiGallery], int]:
        payload = next(
            (
                item
                for item in cls.extract_embedded_payloads(html)
                if isinstance(item.get("result"), list)
            ),
            None,
        )
        if payload is None:
            return [], 1
        raw_results = payload.get("result")
        if not isinstance(raw_results, list):
            return [], 1
        galleries = [
            cls.parse_listing_gallery(item, assumed_author)
            for item in raw_results
            if isinstance(item, dict)
        ]
        num_pages = payload.get("num_pages", 1)
        return galleries, int(num_pages) if isinstance(num_pages, int | str) else 1

    @classmethod
    def parse_listing_gallery(
        cls, payload: dict[str, Any], assumed_author: str
    ) -> NhentaiGallery:
        external_id = str(payload.get("id") or "")
        media_id = str(payload.get("media_id") or "")
        if not external_id.isdigit() or not media_id.isdigit():
            raise ProviderError("nHentai 列表项缺少有效 ID")
        english_title = cls._text(str(payload.get("english_title") or ""))
        japanese_title = cls._text(str(payload.get("japanese_title") or ""))
        alt_titles = list(
            dict.fromkeys(item for item in (english_title, japanese_title) if item)
        )
        thumbnail = payload.get("thumbnail")
        cover_url = (
            cls._image_url("t.nhentai.net", str(thumbnail))
            if isinstance(thumbnail, str) and thumbnail
            else None
        )
        page_count = payload.get("num_pages")
        return NhentaiGallery(
            external_id=external_id,
            media_id=media_id,
            title=english_title or japanese_title or f"#{external_id}",
            cover_url=cover_url,
            image_urls=[],
            language="und",
            upload_date=None,
            artists=[assumed_author],
            tags=[],
            alt_titles=alt_titles,
            scanlator=None,
            page_count=page_count if isinstance(page_count, int) else 0,
        )

    @classmethod
    def parse_artist(cls, html: str) -> str | None:
        for payload in cls.extract_embedded_payloads(html):
            if payload.get("type") == "artist" and payload.get("name"):
                return cls._text(str(payload["name"]))
        return None

    @classmethod
    def extract_gallery_payload(cls, html: str) -> dict[str, Any] | None:
        return next(
            (
                item
                for item in cls.extract_embedded_payloads(html)
                if item.get("media_id") is not None
                and isinstance(item.get("title"), dict)
            ),
            None,
        )

    @staticmethod
    def extract_embedded_payloads(html: str) -> list[dict[str, Any]]:
        payloads: list[dict[str, Any]] = []
        soup = BeautifulSoup(html, "html.parser")
        for script in soup.select('script[type="application/json"]'):
            try:
                outer = json.loads(script.string or script.get_text())
                body = outer.get("body") if isinstance(outer, dict) else None
                inner = json.loads(body) if isinstance(body, str) else body
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(inner, dict):
                payloads.append(inner)
        return payloads

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
        current_pages = payload.get("pages")
        if isinstance(current_pages, list):
            image_urls = [
                cls._image_url("i.nhentai.net", str(page["path"]))
                for page in current_pages
                if isinstance(page, dict) and page.get("path")
            ]
        else:
            raw_pages = images.get("pages") if isinstance(images.get("pages"), list) else []
            image_urls = [
                f"https://i.nhentai.net/galleries/{media_id}/{index}.{cls._extension(page)}"
                for index, page in enumerate(raw_pages, start=1)
                if isinstance(page, dict)
            ]

        current_cover = payload.get("cover")
        if isinstance(current_cover, dict) and current_cover.get("path"):
            cover_url = cls._image_url(
                "t.nhentai.net", str(current_cover["path"])
            )
        else:
            legacy_cover = (
                images.get("cover") if isinstance(images.get("cover"), dict) else None
            )
            cover_url = (
                f"https://t.nhentai.net/galleries/{media_id}/cover.{cls._extension(legacy_cover)}"
                if legacy_cover
                else None
            )
        upload_timestamp = payload.get("upload_date")
        upload_date = (
            datetime.fromtimestamp(upload_timestamp, UTC)
            if isinstance(upload_timestamp, int | float)
            else None
        )
        scanlator = cls._text(str(payload.get("scanlator") or "")) or None
        raw_page_count = payload.get("num_pages")
        page_count = raw_page_count if isinstance(raw_page_count, int) else len(image_urls)
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
            page_count=page_count,
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
                "page_count": gallery.page_count,
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
    def _image_url(host: str, path: str) -> str:
        return f"https://{host}/{path.lstrip('/')}"

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
    def _slug(value: str) -> str:
        normalized = unicodedata.normalize("NFKC", value).casefold()
        slug = re.sub(r"[^\w\u3040-\u30ff\u3400-\u9fff]+", "-", normalized)
        return slug.strip("-")

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
                "just a moment...",
                "attention required! | cloudflare",
                "cf-chl-widget",
            )
        )

    @staticmethod
    def _validate_id(value: str) -> None:
        if not value.isdigit():
            raise ProviderError("nHentai gallery ID 无效")

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
