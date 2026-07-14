import asyncio
import html
import re
import zipfile
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
from app.providers.errors import AuthorNotFoundError, ProviderError


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
    ) -> None:
        if not base_urls:
            raise ValueError("WNACG 至少需要一个候选域名")
        self._base_urls = [url.rstrip("/") for url in base_urls]
        self._active_base_url: str | None = None
        self._max_search_pages = max(1, min(max_search_pages, 20))
        self._owns_client = client is None
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

    async def discover_by_author(self, author_name: str) -> list[DiscoveredWork]:
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
            await asyncio.sleep(0.25)
        if not works:
            raise AuthorNotFoundError(f'WNACG 未找到与“{author_name}”相关的作品')
        unique_works = list({work.external_id: work for work in works}.values())
        return sort_discovered_works(unique_works)

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

    async def fetch_cover(
        self, work_external_id: str, cover_url: str
    ) -> RemoteImage:
        self._validate_id(work_external_id)
        parsed = urlsplit(cover_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or not parsed.hostname.endswith(".wnacgimg.date")
        ):
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
                    try:
                        image_response = await self._client.get(image_url, headers=headers)
                        image_response.raise_for_status()
                    except httpx.HTTPError as exc:
                        raise ProviderError(f"WNACG 第 {index} 页下载失败: {exc}") from exc
                    suffix = Path(image_response.url.path).suffix or ".jpg"
                    archive.writestr(f"{index:04d}{suffix}", image_response.content)
                    await asyncio.sleep(0.08)
            temporary.replace(output)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return str(output)

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
                errors.append(f"{base_url}: {exc}")
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
                    language="zh-Hant",
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
                "challenge-platform",
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
