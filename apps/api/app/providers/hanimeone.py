import asyncio
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Tag

from app.providers.base import Chapter, DiscoveredWork, ProviderCapability
from app.providers.errors import AuthorNotFoundError, ProviderError


@dataclass(frozen=True, slots=True)
class HanimeOneDetail:
    title: str
    cover_url: str | None
    page_count: int | None
    image_urls: dict[int, str]
    tags: list[str]
    language: str
    upload_label: str | None


class HanimeOneProvider:
    name = "hanimeone"
    display_name = "Hanime1 漫画"
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
        base_url: str = "https://hanimeone.me",
        proxy_url: str = "",
        cookie: str = "",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._owns_client = client is None
        headers = {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.6",
        }
        if cookie:
            headers["Cookie"] = cookie
        self._client = client or httpx.AsyncClient(
            headers=headers,
            proxy=proxy_url or None,
            trust_env=False,
            follow_redirects=True,
            timeout=httpx.Timeout(25.0),
            limits=httpx.Limits(max_connections=5),
        )

    async def discover_by_author(self, author_name: str) -> list[DiscoveredWork]:
        response = await self._request(f"/artists/{quote(author_name, safe='')}")
        works = self.parse_author_page(response.text, self._base_url)
        if not works:
            raise AuthorNotFoundError(f'Hanime1 未找到作者“{author_name}”')
        return works

    async def list_chapters(self, work_external_id: str) -> list[Chapter]:
        self._validate_id(work_external_id)
        response = await self._request(f"/comic/{work_external_id}")
        detail = self.parse_detail(response.text, self._base_url, work_external_id)
        return [
            Chapter(
                external_id=work_external_id,
                title="全本",
                number=None,
                language=detail.language,
                published_at=None,
                source_url=f"{self._base_url}/comic/{work_external_id}",
            )
        ]

    async def download_chapter(
        self, work_external_id: str, chapter_external_id: str, destination: str
    ) -> str:
        self._validate_id(work_external_id)
        if chapter_external_id != work_external_id:
            raise ProviderError("Hanime1 章节不属于所选作品")
        response = await self._request(f"/comic/{work_external_id}")
        detail = self.parse_detail(response.text, self._base_url, work_external_id)
        if not detail.page_count:
            raise ProviderError("Hanime1 详情页缺少页数")

        output = Path(destination)
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(output.suffix + ".part")
        try:
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for page in range(1, detail.page_count + 1):
                    image_url = detail.image_urls.get(page)
                    if not image_url:
                        page_response = await self._request(
                            f"/comic/{work_external_id}/{page}"
                        )
                        image_url = self.parse_reader_image(page_response.text)
                    if not image_url:
                        raise ProviderError(f"Hanime1 无法解析第 {page} 页图片")
                    try:
                        image_response = await self._client.get(
                            image_url,
                            headers={
                                "Referer": (
                                    f"{self._base_url}/comic/{work_external_id}/{page}"
                                )
                            },
                        )
                        image_response.raise_for_status()
                    except httpx.HTTPError as exc:
                        raise ProviderError(f"Hanime1 第 {page} 页下载失败: {exc}") from exc
                    suffix = Path(image_response.url.path).suffix or ".jpg"
                    archive.writestr(f"{page:04d}{suffix}", image_response.content)
                    await asyncio.sleep(0.06)
            temporary.replace(output)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return str(output)

    async def _request(self, path: str) -> httpx.Response:
        try:
            response = await self._client.get(
                f"{self._base_url}{path}",
                headers={"Referer": f"{self._base_url}/comics"},
            )
            response.raise_for_status()
            if self._is_challenge(response.text):
                raise ProviderError("Hanime1 触发 Cloudflare，请配置代理或 Cookie")
            return response
        except ProviderError:
            raise
        except httpx.HTTPError as exc:
            raise ProviderError(f"Hanime1 请求失败: {exc}") from exc

    @classmethod
    def parse_author_page(cls, html_text: str, base_url: str) -> list[DiscoveredWork]:
        soup = BeautifulSoup(html_text, "html.parser")
        works: dict[str, DiscoveredWork] = {}
        for anchor in soup.select('a[href*="/comic/"]'):
            if not isinstance(anchor, Tag):
                continue
            href = str(anchor.get("href") or "")
            match = re.search(r"/comic/(\d+)/?$", urlparse(href).path)
            if not match:
                continue
            external_id = match.group(1)
            title = cls._text(str(anchor.get("title") or anchor.get_text(" ")))
            image = anchor.find("img")
            if not title and isinstance(image, Tag):
                title = cls._text(str(image.get("alt") or ""))
            if not title:
                continue
            cover_url = cls._image_value(image, base_url)
            works[external_id] = DiscoveredWork(
                external_id=external_id,
                title=title,
                source_url=f"{base_url.rstrip('/')}/comic/{external_id}",
                cover_url=cover_url,
                status="ongoing" if re.search(r"ongoing|持續更新|持续更新", title, re.I) else None,
                language="zh-Hant",
                raw_metadata={"author_route": urlparse(str(anchor.get("href") or "")).path},
            )
        return list(works.values())

    @classmethod
    def parse_detail(
        cls, html_text: str, base_url: str, external_id: str
    ) -> HanimeOneDetail:
        soup = BeautifulSoup(html_text, "html.parser")
        headings = [cls._text(item.get_text(" ")) for item in soup.select("h3, h4")]
        title = next((value for value in reversed(headings) if value), f"#{external_id}")
        text = " ".join(soup.stripped_strings)
        page_match = re.search(r"頁數\s*[：:]?\s*(\d+)", text)
        upload_match = re.search(
            r"上傳\s*[：:]?\s*([^#]{1,30}?)(?=\s*(?:下載|download|$))",
            text,
            re.I,
        )
        tags = [cls._text(anchor.get_text(" ")) for anchor in soup.select('a[href*="/tags/"]')]
        languages = [
            cls._text(anchor.get_text(" "))
            for anchor in soup.select('a[href*="/languages/"]')
        ]
        language = "zh-Hant" if any("中文" in value for value in languages) else "und"
        image_urls: dict[int, str] = {}
        cover_url: str | None = None
        for anchor in soup.select(f'a[href*="/comic/{external_id}/"]'):
            if not isinstance(anchor, Tag):
                continue
            page_match_href = re.search(
                rf"/comic/{re.escape(external_id)}/(\d+)/?$",
                urlparse(str(anchor.get("href") or "")).path,
            )
            image = anchor.find("img")
            value = cls._image_value(image, base_url)
            if value and cover_url is None:
                cover_url = value
            if page_match_href and value:
                full = cls._thumbnail_to_full(value)
                if full:
                    image_urls[int(page_match_href.group(1))] = full
        return HanimeOneDetail(
            title=title,
            cover_url=cover_url,
            page_count=int(page_match.group(1)) if page_match else None,
            image_urls=image_urls,
            tags=[tag for tag in dict.fromkeys(tags) if tag],
            language=language,
            upload_label=upload_match.group(1).strip() if upload_match else None,
        )

    @staticmethod
    def parse_reader_image(html_text: str) -> str | None:
        soup = BeautifulSoup(html_text, "html.parser")
        for image in soup.find_all("img"):
            value = HanimeOneProvider._image_value(image, "https://hanimeone.me")
            if value and re.search(r"https://i\d+\.nhentai\.net/galleries/", value):
                return value
        return None

    @staticmethod
    def _thumbnail_to_full(url: str) -> str | None:
        if not re.search(r"https://t\d+\.nhentai\.net/galleries/", url):
            return None
        value = re.sub(r"https://t(\d+)\.", r"https://i\1.", url)
        return re.sub(r"/(\d+)t(\.[a-zA-Z0-9]+)$", r"/\1\2", value)

    @staticmethod
    def _image_value(image: Tag | None, base_url: str) -> str | None:
        if not isinstance(image, Tag):
            return None
        # Hanime1 的列表页把真实封面放在 data-srcset，src 只是所有作品
        # 共用的占位图。srcset 可能含多组带尺寸描述符的候选地址，封面取首项即可。
        for name in ("data-srcset", "srcset", "data-src", "data-lazy-src", "src"):
            value = str(image.get(name) or "").strip()
            if name.endswith("srcset") and value:
                value = value.split(",", 1)[0].strip().split(" ", 1)[0]
            if value and value != "undefined" and not value.endswith("/undefined"):
                return urljoin(f"{base_url.rstrip('/')}/", value)
        return None

    @staticmethod
    def _text(value: str) -> str:
        return " ".join(value.split()).strip()

    @staticmethod
    def _is_challenge(text: str) -> bool:
        lowered = text.casefold()
        return "attention required! | cloudflare" in lowered or "cf-error-details" in lowered

    @staticmethod
    def _validate_id(value: str) -> None:
        if not value.isdigit():
            raise ProviderError("Hanime1 作品 ID 无效")

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
