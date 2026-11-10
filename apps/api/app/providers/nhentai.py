import asyncio
import re
import time
import unicodedata
import zipfile
from collections import Counter, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from app.providers.base import (
    Chapter,
    DiscoveredWork,
    ProviderCapability,
    RemoteImage,
    sort_discovered_works,
)
from app.providers.errors import (
    AuthorIdentityUnconfirmedError,
    AuthorNotFoundError,
    ProviderError,
    describe_http_error,
)

Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]


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
    tag_ids: list[int] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class NhentaiArtistTag:
    id: int
    name: str


class _RateLimiter:
    """Sliding-window limiter matching nHentai's documented per-minute API quotas."""

    def __init__(self, limit: int, sleep: Sleep, clock: Clock, window: float = 60.0) -> None:
        self._limit = max(1, limit)
        self._window = window
        self._sleep = sleep
        self._clock = clock
        self._calls: deque[float] = deque()
        self._blocked_until = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = self._clock()
                if self._blocked_until > now:
                    await self._sleep(self._blocked_until - now)
                    continue
                while self._calls and now - self._calls[0] >= self._window:
                    self._calls.popleft()
                if len(self._calls) < self._limit:
                    self._calls.append(now)
                    return
                await self._sleep(self._window - (now - self._calls[0]) + 0.05)

    def block_for(self, seconds: float) -> None:
        self._blocked_until = max(self._blocked_until, self._clock() + seconds)


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

    # Documented per-IP quotas of https://nhentai.net/api/v2/docs, kept slightly
    # below the published values so concurrent scans do not trip HTTP 429.
    _anonymous_limits = {"search": 9, "gallery": 18, "tag": 13, "tagged": 13}
    _api_key_limits = {"search": 18, "gallery": 40, "tag": 27, "tagged": 27}
    _max_rate_limit_retries = 2
    _max_retry_after_seconds = 90.0
    _tagged_page_size = 100
    _detail_limit = 8
    _tag_lookup_batch = 100

    def __init__(
        self,
        user_agent: str,
        base_url: str = "https://nhentai.net",
        proxy_url: str = "",
        cookie: str = "",
        api_key: str = "",
        max_search_pages: int = 3,
        client: httpx.AsyncClient | None = None,
        sleep: Sleep = asyncio.sleep,
        clock: Clock = time.monotonic,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._max_search_pages = max(1, min(max_search_pages, 20))
        self._owns_client = client is None
        self._sleep = sleep
        headers = {
            "User-Agent": user_agent,
            "Accept": "application/json, image/*;q=0.8, */*;q=0.5",
            "Accept-Language": "en-US,en;q=0.9,ja;q=0.7",
        }
        if cookie:
            headers["Cookie"] = cookie
        self._api_headers = {"Authorization": f"Key {api_key.strip()}"} if api_key.strip() else {}
        limits = self._api_key_limits if self._api_headers else self._anonymous_limits
        self._limiters = {
            bucket: _RateLimiter(limit, sleep, clock) for bucket, limit in limits.items()
        }
        self._client = client or httpx.AsyncClient(
            headers=headers,
            proxy=proxy_url or None,
            trust_env=False,
            follow_redirects=True,
            timeout=httpx.Timeout(30.0),
            limits=httpx.Limits(max_connections=5),
        )

    async def discover_by_author(
        self,
        author_name: str,
        *,
        author_names: set[str] | None = None,
    ) -> list[DiscoveredWork]:
        normalized_author = " ".join(author_name.split()).strip()
        artist = await self._find_artist_tag(normalized_author, author_names=author_names)

        galleries: dict[str, NhentaiGallery] = {}
        total_pages = 1
        page = 1
        while page <= min(total_pages, self._max_search_pages):
            payload = await self._request_api(
                "/api/v2/galleries/tagged",
                bucket="tagged",
                params={
                    "tag_id": str(artist.id),
                    "sort": "date",
                    "page": str(page),
                    "per_page": str(self._tagged_page_size),
                },
            )
            parsed, total_pages = self.parse_listing(payload, artist.name)
            for gallery in parsed:
                galleries.setdefault(gallery.external_id, gallery)
            page += 1

        if not galleries:
            raise AuthorNotFoundError(f"nHentai 未找到作者“{author_name}”")

        semaphore = asyncio.Semaphore(2)

        async def enrich(gallery: NhentaiGallery) -> NhentaiGallery:
            # The tag listing is authoritative, so details only add metadata. A
            # failed detail lookup keeps the listed gallery instead of failing the scan.
            async with semaphore:
                for attempt in range(2):
                    try:
                        detailed = await self._gallery(gallery.external_id)
                    except ProviderError:
                        if attempt == 1:
                            return gallery
                        await self._sleep(0.35)
                        continue
                    return detailed if detailed.artists else gallery
            return gallery

        listed = list(galleries.values())
        detail_limit = min(self._detail_limit, len(listed))
        enriched = await asyncio.gather(*(enrich(item) for item in listed[:detail_limit]))
        exact = [*enriched, *listed[detail_limit:]]
        return sort_discovered_works([self._discovered_work(gallery) for gallery in exact])

    async def _find_artist_tag(
        self, author_name: str, *, author_names: set[str] | None = None
    ) -> NhentaiArtistTag:
        """Resolve an artist tag, accepting site-specific aliases only when trusted."""

        slug = self._slug(author_name)
        if slug:
            try:
                tag = await self._request_api(
                    f"/api/v2/tags/artist/{quote(slug, safe='')}", bucket="tag"
                )
                artist = self.parse_artist(tag)
                if artist and self._identity(artist.name) == self._identity(author_name):
                    return artist
            except ProviderError as exc:
                # Only a missing artist tag justifies searching for an alternate name.
                # Authentication, rate limits and transport failures affect the source,
                # so retrying another endpoint only creates extra requests and hides them.
                cause = exc.__cause__
                missing = (
                    isinstance(cause, httpx.HTTPStatusError) and cause.response.status_code == 404
                )
                if not missing:
                    raise

        trusted = {self._identity(name) for name in (author_names or set()) | {author_name}}
        trusted.discard("")
        search = await self._request_api(
            "/api/v2/search",
            bucket="search",
            params={"query": f'artist:"{author_name}"', "sort": "date", "page": "1"},
        )
        exact_galleries, _ = self.parse_listing(search, author_name)
        if exact_galleries:
            artists = await self._artist_tags(self._tag_frequency(exact_galleries))
            matching = [item for item in artists if self._identity(item.name) in trusted]
            own_identity = self._identity(author_name)
            preferred = [item for item in matching if self._identity(item.name) == own_identity]
            if preferred or matching:
                return (preferred or matching)[0]

        alias = await self._discover_author_alias(author_name)
        if alias is None:
            raise AuthorNotFoundError(f"nHentai 未找到作者“{author_name}”")
        if self._identity(alias.name) not in trusted:
            raise AuthorIdentityUnconfirmedError(author_name, alias.name)
        return alias

    async def _discover_author_alias(self, author_name: str) -> NhentaiArtistTag | None:
        """Infer one dominant real artist tag from a bounded plain-search sample."""

        payload = await self._request_api(
            "/api/v2/search",
            bucket="search",
            params={"query": author_name, "sort": "date", "page": "1"},
        )
        candidates, _ = self.parse_listing(payload, author_name)
        sample = [item for item in candidates[:8] if item.tag_ids]
        if len(sample) < 2:
            return None

        frequency = self._tag_frequency(sample)
        recurring = Counter({tag_id: count for tag_id, count in frequency.items() if count >= 2})
        if not recurring:
            return None
        artists = await self._artist_tags(recurring)
        counts: Counter[str] = Counter()
        display: dict[str, NhentaiArtistTag] = {}
        for artist in artists:
            identity = self._identity(artist.name)
            if identity:
                counts[identity] += frequency[artist.id]
                display.setdefault(identity, artist)
        if not counts:
            return None

        ranked = counts.most_common(2)
        identity, support = ranked[0]
        tied = len(ranked) > 1 and ranked[1][1] == support
        if support < 2 or support / len(sample) < 0.6 or tied:
            return None
        return display[identity]

    @staticmethod
    def _tag_frequency(galleries: list[NhentaiGallery]) -> Counter[int]:
        counts: Counter[int] = Counter()
        for gallery in galleries:
            counts.update(set(gallery.tag_ids))
        return counts

    async def _artist_tags(self, frequency: Counter[int]) -> list[NhentaiArtistTag]:
        """Look up the most frequent tags in one request and keep artist tags."""

        ids = [tag_id for tag_id, _ in frequency.most_common(self._tag_lookup_batch)]
        if not ids:
            return []
        payload = await self._request_api(
            "/api/v2/tags/ids",
            bucket="tag",
            params={"ids": ",".join(str(tag_id) for tag_id in ids)},
        )
        if not isinstance(payload, list):
            raise ProviderError("nHentai 标签数据缺失，接口结构可能已变化")
        artists = [self.parse_artist(item) for item in payload if isinstance(item, dict)]
        return [item for item in artists if item is not None]

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

    async def fetch_cover(self, work_external_id: str, cover_url: str) -> RemoteImage:
        self._validate_id(work_external_id)
        parsed = urlsplit(cover_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or not (parsed.hostname == "nhentai.net" or parsed.hostname.endswith(".nhentai.net"))
            or not parsed.path.startswith("/galleries/")
        ):
            raise ProviderError("nHentai 封面地址不受信任")
        try:
            response = await self._client.get(
                cover_url,
                headers={"Referer": f"{self._base_url}/g/{work_external_id}/"},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ProviderError(f"nHentai 封面下载失败: {exc}") from exc
        return self._remote_image(response)

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
                        response = await self._client.get(image_url, headers={"Referer": referer})
                        response.raise_for_status()
                    except httpx.HTTPError as exc:
                        raise ProviderError(f"nHentai 第 {index} 页下载失败: {exc}") from exc
                    suffix = Path(response.url.path).suffix or Path(image_url).suffix or ".jpg"
                    archive.writestr(f"{index:04d}{suffix}", response.content)
                    await self._sleep(0.06)
            temporary.replace(output)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return str(output)

    async def _gallery(self, external_id: str) -> NhentaiGallery:
        self._validate_id(external_id)
        payload = await self._request_api(f"/api/v2/galleries/{external_id}", bucket="gallery")
        if (
            not isinstance(payload, dict)
            or payload.get("media_id") is None
            or not isinstance(payload.get("title"), dict)
        ):
            raise ProviderError("nHentai 详情缺少作品数据，接口结构可能已变化")
        return self.parse_gallery(payload)

    async def _request_api(
        self,
        path: str,
        *,
        bucket: str,
        params: dict[str, str] | None = None,
    ) -> Any:
        """Call the official JSON API with quota pacing and Retry-After handling.

        The HTML pages sit behind a Cloudflare JavaScript challenge that a server
        cannot pass with any User-Agent; the documented API is served without it.
        """

        limiter = self._limiters[bucket]
        for attempt in range(self._max_rate_limit_retries + 1):
            await limiter.acquire()
            try:
                response = await self._client.get(
                    f"{self._base_url}{path}",
                    params=params,
                    headers=self._api_headers,
                )
                if response.status_code == 429:
                    wait = self._retry_after(response)
                    limiter.block_for(min(wait, self._max_retry_after_seconds))
                    retryable = wait <= self._max_retry_after_seconds
                    if retryable and attempt < self._max_rate_limit_retries:
                        continue
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                detail = describe_http_error(exc)
                if exc.response.status_code in {403, 503} and self._is_challenge(exc.response.text):
                    detail += "；触发 Cloudflare 验证，请稍后重试或更换代理出口"
                raise ProviderError(f"nHentai 请求失败: {detail}") from exc
            except httpx.HTTPError as exc:
                raise ProviderError(f"nHentai 请求失败: {describe_http_error(exc)}") from exc

            try:
                return response.json()
            except ValueError as exc:
                if self._is_challenge(response.text):
                    raise ProviderError(
                        "nHentai 触发 Cloudflare 验证，请稍后重试或更换代理出口"
                    ) from exc
                raise ProviderError("nHentai 返回的不是 JSON，接口结构可能已变化") from exc
        raise ProviderError("nHentai 请求失败: HTTP 429（请求过于频繁）")

    @staticmethod
    def _retry_after(response: httpx.Response) -> float:
        try:
            return max(1.0, float(response.headers.get("retry-after", "60")))
        except ValueError:
            return 60.0

    @classmethod
    def parse_listing(cls, payload: Any, assumed_author: str) -> tuple[list[NhentaiGallery], int]:
        if not isinstance(payload, dict) or not isinstance(payload.get("result"), list):
            raise ProviderError("nHentai 列表数据缺失，接口结构可能已变化")
        galleries = [
            cls.parse_listing_gallery(item, assumed_author)
            for item in payload["result"]
            if isinstance(item, dict)
        ]
        num_pages = payload.get("num_pages", 1)
        try:
            pages = int(num_pages)
        except (TypeError, ValueError):
            pages = 1
        return galleries, max(1, pages)

    @classmethod
    def parse_listing_gallery(cls, payload: dict[str, Any], assumed_author: str) -> NhentaiGallery:
        external_id = str(payload.get("id") or "")
        media_id = str(payload.get("media_id") or "")
        if not external_id.isdigit() or not media_id.isdigit():
            raise ProviderError("nHentai 列表项缺少有效 ID")
        english_title = cls._text(str(payload.get("english_title") or ""))
        japanese_title = cls._text(str(payload.get("japanese_title") or ""))
        alt_titles = list(dict.fromkeys(item for item in (english_title, japanese_title) if item))
        thumbnail = payload.get("thumbnail")
        cover_url = (
            cls._image_url("t.nhentai.net", str(thumbnail))
            if isinstance(thumbnail, str) and thumbnail
            else None
        )
        page_count = payload.get("num_pages")
        raw_tag_ids = payload.get("tag_ids")
        tag_ids = (
            [item for item in raw_tag_ids if isinstance(item, int)]
            if isinstance(raw_tag_ids, list)
            else []
        )
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
            tag_ids=tag_ids,
        )

    @classmethod
    def parse_artist(cls, payload: Any) -> NhentaiArtistTag | None:
        if not isinstance(payload, dict) or payload.get("type") != "artist":
            return None
        tag_id = payload.get("id")
        name = cls._text(str(payload.get("name") or ""))
        if not isinstance(tag_id, int) or not name:
            return None
        return NhentaiArtistTag(id=tag_id, name=name)

    @classmethod
    def parse_search(cls, payload: dict[str, Any]) -> tuple[list[NhentaiGallery], int]:
        raw_results = payload.get("result")
        if not isinstance(raw_results, list):
            return [], 1
        galleries = [cls.parse_gallery(item) for item in raw_results if isinstance(item, dict)]
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
            cls._text(str(titles.get(key) or "")) for key in ("english", "japanese", "pretty")
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
            cover_url = cls._image_url("t.nhentai.net", str(current_cover["path"]))
        else:
            legacy_cover = images.get("cover") if isinstance(images.get("cover"), dict) else None
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
                "uploaded_at": (gallery.upload_date.isoformat() if gallery.upload_date else None),
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

    @staticmethod
    def _remote_image(response: httpx.Response) -> RemoteImage:
        content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
        if not content_type.startswith("image/") or not response.content:
            raise ProviderError("nHentai 封面响应不是有效图片")
        if len(response.content) > 8 * 1024 * 1024:
            raise ProviderError("nHentai 封面超过 8 MB 限制")
        return RemoteImage(content=response.content, content_type=content_type)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
