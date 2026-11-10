import asyncio
import html
import re
import unicodedata
import zipfile
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup, Tag

from app.providers.base import (
    Chapter,
    DiscoveredWork,
    ProviderCapability,
    RemoteImage,
    sort_discovered_works,
)
from app.providers.errors import AuthorNotFoundError, ProviderError, describe_http_error

Sleep = Callable[[float], Awaitable[None]]

class WnacgProvider:
    name = "wnacg"
    display_name = "WNACG"
    capabilities = frozenset(
        {
            ProviderCapability.AUTHOR_DISCOVERY,
            ProviderCapability.CHAPTER_LIST,
            ProviderCapability.DOWNLOAD,
        }
    )
    canonical_url = "https://www.wnacg.com"

    # Leading event / release-type markers, e.g. "(C85) (同人誌) [Circle (Artist)]".
    _event_prefix_re = re.compile(
        r"^\s*[（(](?:c\d+[a-z]?|comiket\s*\d+|comic\s*market\s*\d+|"
        r"ac\d+|comitia\s*\d+|コミティア\s*\d+|comic1\s*☆?\s*\d+|"
        r"sc\d+|例大祭\s*\d*|紅楼夢\s*\d*|ff\d+|cr\d+|"
        r"同人誌|同人cg集|成年コミック|一般コミック)[）)]\s*",
        re.IGNORECASE,
    )
    _creator_prefix_re = re.compile(r"^\s*[\[【]([^\]】]{1,160})[\]】]")
    _credit_separator_re = re.compile(r"[/／|｜,，、;；&＆+＋×]")
    _non_creator_re = re.compile(
        r"汉化|漢化|翻译|翻譯|翻訳|扫图|掃圖|嵌字|机翻|機翻|校对|校對|"
        r"修图|修圖|去码|去碼|無修正|无修正|scan(?:lation)?|translat",
        re.IGNORECASE,
    )

    _category_names = {
        "cate-1": "同人誌 / 漢化",
        "cate-5": "同人誌",
        "cate-6": "單行本",
        "cate-7": "雜誌&短篇",
        "cate-9": "單行本 / 漢化",
        "cate-10": "雜誌&短篇 / 漢化",
        "cate-12": "同人誌 / 日語",
        "cate-13": "單行本 / 日語",
        "cate-14": "雜誌&短篇 / 日語",
        "cate-16": "同人誌 / English",
        "cate-17": "單行本 / English",
        "cate-18": "雜誌&短篇 / English",
        "cate-19": "韓漫",
        "cate-20": "韓漫 / 漢化",
        "cate-21": "韓漫 / 其他",
        "cate-22": "同人誌 / 3D漫畫",
        "cate-37": "同人誌 / AI圖集",
    }

    def __init__(
        self,
        user_agent: str,
        base_urls: list[str],
        cookie: str = "",
        max_search_pages: int = 5,
        client: httpx.AsyncClient | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if not base_urls:
            raise ValueError("WNACG 至少需要一个候选域名")
        self._base_urls = [url.rstrip("/") for url in base_urls]
        self._active_base_url: str | None = None
        self._max_search_pages = max(1, min(max_search_pages, 20))
        self._owns_client = client is None
        self._sleep = sleep
        headers = {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,zh-TW;q=0.8,en;q=0.5",
        }
        if cookie:
            headers["Cookie"] = cookie
        self._client = client or httpx.AsyncClient(
            headers=headers,
            follow_redirects=True,
            timeout=httpx.Timeout(25.0),
            limits=httpx.Limits(max_connections=4),
        )

    async def discover_by_author(
        self, author_name: str, *, author_names: set[str] | None = None,
    ) -> list[DiscoveredWork]:
        first_response, base_url = await self._request_site(
            "/search/",
            params={
                "f": "_all",
                "q": author_name,
                "s": "create_time_DESC",
                "syn": "yes",
            },
        )
        works, page_count = self.parse_search(first_response.text, base_url)
        for page in range(2, min(page_count, self._max_search_pages) + 1):
            response, _ = await self._request_site(
                "/search/index.php",
                params={
                    "f": "_all",
                    "q": author_name,
                    "s": "create_time_DESC",
                    "syn": "yes",
                    "p": str(page),
                },
            )
            page_works, _ = self.parse_search(response.text, base_url)
            works.extend(page_works)
            await self._sleep(0.25)
        unique_works = list({work.external_id: work for work in works}.values())
        all_names = author_names or {author_name}
        verified = [
            replace(
                work,
                raw_metadata={
                    **work.raw_metadata,
                    "author_match": "creator_credit_exact",
                    "author_query": author_name,
                },
            )
            for work in unique_works
            if self.matches_creator_credit(work.title, author_name, all_names)
        ]
        if not verified:
            raise AuthorNotFoundError(
                f'WNACG 未找到作者署名与“{author_name}”一致的作品；'
                "仅在汉化组、发布者或标题正文中命中不会导入"
            )
        return sort_discovered_works(verified)

    async def list_chapters(self, work_external_id: str) -> list[Chapter]:
        self._validate_id(work_external_id)
        return [
            Chapter(
                external_id=work_external_id,
                title="全本",
                number=None,
                language="zh-Hant",
                published_at=None,
                source_url=(
                    f"{self.canonical_url}/photos-index-aid-{work_external_id}.html"
                ),
            )
        ]

    _trusted_cover_domains: tuple[str, ...] = (
        ".wnacgimg.date",
        ".qy0.ru",
        ".wnimg2.cfd",
    )

    def _is_trusted_cover_url(self, cover_url: str) -> bool:
        parsed = urlsplit(cover_url)
        if parsed.scheme != "https" or not parsed.hostname:
            return False
        hostname = parsed.hostname.lower()
        if any(
            hostname == domain.lstrip(".") or hostname.endswith(domain)
            for domain in self._trusted_cover_domains
        ):
            return True
        base_hosts = {
            urlsplit(base_url).hostname.lower()
            for base_url in self._base_urls
            if urlsplit(base_url).hostname
        }
        return hostname in base_hosts

    async def fetch_cover(
        self, work_external_id: str, cover_url: str
    ) -> RemoteImage:
        self._validate_id(work_external_id)
        if not self._is_trusted_cover_url(cover_url):
            raise ProviderError("WNACG 封面地址不受信任")
        try:
            response = await self._client.get(
                cover_url,
                headers={
                    "Referer": (
                        f"{self.canonical_url}/photos-index-aid-{work_external_id}.html"
                    )
                },
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ProviderError(f"WNACG 封面下载失败: {exc}") from exc
        content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
        if not content_type.startswith("image/") or not response.content:
            raise ProviderError("WNACG 封面响应不是有效图片")
        if len(response.content) > 8 * 1024 * 1024:
            raise ProviderError("WNACG 封面超过 8 MB 限制")
        return RemoteImage(content=response.content, content_type=content_type)

    async def download_chapter(
        self, work_external_id: str, chapter_external_id: str, destination: str
    ) -> str:
        self._validate_id(work_external_id)
        if chapter_external_id != work_external_id:
            raise ProviderError("WNACG 章节不属于所选作品")
        response, base_url = await self._request_site(
            f"/photos-gallery-aid-{work_external_id}.html"
        )
        image_urls = self.parse_gallery_images(response.text)
        if not image_urls:
            raise ProviderError("WNACG 图片清单为空或页面结构已变化")

        output = Path(destination)
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(output.suffix + ".part")
        headers = {"Referer": f"{base_url}/photos-index-aid-{work_external_id}.html"}
        try:
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for index, image_url in enumerate(image_urls, start=1):
                    image_response = await self._download_image(index, image_url, headers)
                    suffix = Path(image_response.url.path).suffix or ".jpg"
                    archive.writestr(f"{index:04d}{suffix}", image_response.content)
                    await self._sleep(0.08)
            temporary.replace(output)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return str(output)

    _image_attempts = 3

    async def _download_image(
        self, index: int, image_url: str, headers: dict[str, str]
    ) -> httpx.Response:
        """Fetch one page, retrying transient failures so long galleries can finish."""

        for attempt in range(1, self._image_attempts + 1):
            try:
                response = await self._client.get(image_url, headers=headers)
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                transient = status == 429 or status >= 500
                if not transient or attempt == self._image_attempts:
                    raise ProviderError(
                        f"WNACG 第 {index} 页下载失败: {describe_http_error(exc)}"
                    ) from exc
            except httpx.HTTPError as exc:
                if attempt == self._image_attempts:
                    raise ProviderError(
                        f"WNACG 第 {index} 页下载失败: {describe_http_error(exc)}"
                    ) from exc
            else:
                content_type = response.headers.get("content-type", "").lower()
                if content_type.startswith("text/html") or not response.content:
                    raise ProviderError(f"WNACG 第 {index} 页不是有效图片")
                return response
            await self._sleep(1.5 * attempt)
        raise ProviderError(f"WNACG 第 {index} 页下载失败")

    async def _request_site(
        self, path: str, params: dict[str, str] | None = None
    ) -> tuple[httpx.Response, str]:
        candidates = (
            [self._active_base_url]
            if self._active_base_url
            else []
        ) + [url for url in self._base_urls if url != self._active_base_url]
        errors: list[str] = []
        for base_url in candidates:
            if base_url is None:
                continue
            try:
                response = await self._client.get(
                    f"{base_url}{path}", params=params, headers={"Referer": f"{base_url}/"}
                )
                response.raise_for_status()
                if self._is_challenge(response.text):
                    errors.append(f"{base_url}: Cloudflare/重定向挑战")
                    continue
                self._active_base_url = base_url
                return response, base_url
            except httpx.HTTPError as exc:
                host = urlsplit(base_url).hostname or "未知域名"
                errors.append(f"{host}: {describe_http_error(exc)}")
        raise ProviderError("WNACG 所有候选域名均不可用；" + "; ".join(errors))

    @classmethod
    def parse_search(
        cls, html_text: str, base_url: str
    ) -> tuple[list[DiscoveredWork], int]:
        soup = BeautifulSoup(html_text, "html.parser")
        works: list[DiscoveredWork] = []
        for item in soup.select("li.gallary_item"):
            if not isinstance(item, Tag):
                continue
            anchor = item.select_one('.pic_box a[href*="photos-index-aid-"]')
            if not isinstance(anchor, Tag):
                continue
            href = str(anchor.get("href") or "")
            id_match = re.search(r"photos-index-aid-(\d+)\.html", href)
            if not id_match:
                continue
            external_id = id_match.group(1)
            title_anchor = item.select_one(".info .title a") or anchor
            title = cls._clean_text(str(title_anchor.get("title") or title_anchor.get_text(" ")))
            info_text = item.select_one(".info_col")
            metadata_text = info_text.get_text(" ", strip=True) if info_text else ""
            page_match = re.search(r"(\d+)[張张]", metadata_text)
            date_match = re.search(r"(\d{4}-\d{2}-\d{2})", metadata_text)
            category_element = item.select_one(".pic_box")
            classes = category_element.get("class", []) if category_element else []
            category = next(
                (cls._category_names[value] for value in classes if value in cls._category_names),
                None,
            )
            image = anchor.find("img")
            cover_value = str(image.get("src") or "") if isinstance(image, Tag) else ""
            cover_url = cls._absolute_resource(cover_value, base_url) if cover_value else None
            published = (
                datetime.strptime(date_match.group(1), "%Y-%m-%d").replace(tzinfo=UTC)
                if date_match
                else None
            )
            tags = [category] if category else []
            works.append(
                DiscoveredWork(
                    external_id=external_id,
                    title=title,
                    source_url=f"{cls.canonical_url}/photos-index-aid-{external_id}.html",
                    cover_url=cover_url,
                    status="completed" if "完結" in title or "完结" in title else None,
                    # WNACG labels this as creation/upload time, not original release year.
                    year=None,
                    language=cls._language(category),
                    tags=tags,
                    source_updated_at=published,
                    raw_metadata={
                        "page_count": int(page_match.group(1)) if page_match else None,
                        "category": category,
                        "mirror_url": urljoin(f"{base_url}/", href),
                        "uploaded_at": published.isoformat() if published else None,
                    },
                )
            )
        pages = [
            int(match.group(1))
            for anchor in soup.select(".paginator a[href]")
            if (match := re.search(r"[?&]p=(\d+)", str(anchor.get("href") or "")))
        ]
        return works, max(pages, default=1)

    @staticmethod
    def _language(category: str | None) -> str:
        # Category suffixes state the edition language; unlabeled categories on this
        # site are Chinese uploads.
        if category and "日語" in category:
            return "ja"
        if category and "English" in category:
            return "en"
        return "zh-Hant"

    @classmethod
    def matches_creator_credit(
        cls, title: str, author_name: str, all_names: set[str] | None = None,
    ) -> bool:
        """Only trust a leading creator credit, skipping scanlator/translator brackets."""

        check_names = all_names or {author_name}
        value = unicodedata.normalize("NFKC", html.unescape(title)).strip()
        value = cls._strip_event_prefixes(value)

        # Skip non-creator brackets (scanlation/translation groups) to find
        # the real creator credit.
        credit: str | None = None
        for _attempt in range(5):
            match = cls._creator_prefix_re.match(value)
            if match is None:
                return False
            candidate = " ".join(match.group(1).split()).strip()
            if not candidate:
                return False
            rest = value[match.end():].lstrip()
            after_event = cls._strip_event_prefixes(rest)
            # Releases are credited as "(event) [Circle (Artist)] Title"; a bracket
            # placed before the event marker is a release/scanlation group prefix.
            group_prefix = after_event != rest and cls._creator_prefix_re.match(after_event)
            if cls._non_creator_re.search(candidate) or group_prefix:
                value = after_event
                continue
            credit = candidate
            break

        if credit is None:
            return False

        candidates = {credit}
        candidates.update(re.findall(r"[（(]([^）)]{1,100})[）)]", credit))
        without_parenthetical = re.sub(r"[（(][^）)]{1,100}[）)]", " ", credit).strip()
        if without_parenthetical:
            candidates.add(without_parenthetical)
        for v in tuple(candidates):
            candidates.update(
                part.strip()
                for part in cls._credit_separator_re.split(v)
                if part.strip()
            )
        return any(
            (identity := cls._identity(name))
            and any(cls._identity(item) == identity for item in candidates)
            for name in check_names
        )

    @classmethod
    def _strip_event_prefixes(cls, value: str) -> str:
        for _ in range(3):
            stripped = cls._event_prefix_re.sub("", value, count=1)
            if stripped == value:
                break
            value = stripped
        return value

    @staticmethod
    def _identity(value: str) -> str:
        normalized = unicodedata.normalize("NFKC", value).casefold()
        return re.sub(r"[^\w\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]+", "", normalized)

    @staticmethod
    def parse_gallery_images(html_text: str) -> list[str]:
        urls = re.findall(r'url:\s*fast_img_host\+\\?"([^"\\]+)\\?"', html_text)
        normalized: list[str] = []
        for value in urls:
            value = html.unescape(value).replace("\\/", "/")
            if value.startswith("/themes/"):
                continue
            if value.startswith("//"):
                value = f"https:{value}"
            if value.startswith("http://"):
                value = f"https://{value.removeprefix('http://')}"
            if value.startswith("https://") and value not in normalized:
                normalized.append(value)
        return normalized

    @staticmethod
    def _clean_text(value: str) -> str:
        return " ".join(BeautifulSoup(html.unescape(value), "html.parser").get_text("").split())

    @staticmethod
    def _absolute_resource(value: str, base_url: str) -> str:
        if value.startswith("//"):
            return f"https:{value}"
        return urljoin(f"{base_url}/", value)

    @staticmethod
    def _is_challenge(text: str) -> bool:
        lowered = text.casefold()
        return any(
            marker in lowered
            for marker in (
                "<title>just a moment...</title>",
                "<title>redirecting...</title>",
                "<title>attention required! | cloudflare</title>",
                # Normal Cloudflare pages also load /cdn-cgi/challenge-platform/ scripts,
                # so only markers unique to an interstitial challenge are used.
                "window._cf_chl_opt",
                "cf-chl-widget",
                "router.parklogic.com",
            )
        )

    @staticmethod
    def _validate_id(value: str) -> None:
        if not value.isdigit():
            raise ProviderError("WNACG 作品 ID 无效")

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
