from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from app.core.config import Settings


@dataclass(frozen=True, slots=True)
class ScanTriggerResult:
    target: str
    accepted: bool
    status_code: int
    message: str


class LibraryScanService:
    def __init__(
        self, settings: Settings, client: httpx.AsyncClient | None = None
    ) -> None:
        self.settings = settings
        self.client = client

    async def trigger(self, target: str) -> ScanTriggerResult:
        if target == "komga":
            return await self._komga()
        if target == "kavita":
            return await self._kavita()
        raise ValueError("只支持 komga 或 kavita")

    async def _komga(self) -> ScanTriggerResult:
        base = self._base_url(self.settings.komga_base_url, "Komga")
        library = self.settings.komga_library_id.strip()
        if not library:
            raise ValueError("尚未配置 Komga library ID")
        headers = {}
        if self.settings.komga_api_key:
            headers["X-API-Key"] = self.settings.komga_api_key
        response = await self._post(
            f"{base}/api/v1/libraries/{library}/scan", headers=headers
        )
        return self._result("komga", response)

    async def _kavita(self) -> ScanTriggerResult:
        base = self._base_url(self.settings.kavita_base_url, "Kavita")
        library = self.settings.kavita_library_id.strip()
        if not library:
            raise ValueError("尚未配置 Kavita library ID")
        headers = {}
        if self.settings.kavita_api_key:
            headers["Authorization"] = f"Bearer {self.settings.kavita_api_key}"
        response = await self._post(
            f"{base}/api/Library/scan", params={"libraryId": library}, headers=headers
        )
        return self._result("kavita", response)

    async def _post(self, url: str, **kwargs: object) -> httpx.Response:
        if self.client:
            return await self.client.post(url, **kwargs)
        async with httpx.AsyncClient(
            timeout=self.settings.integration_timeout_seconds,
            follow_redirects=False,
        ) as client:
            return await client.post(url, **kwargs)

    @staticmethod
    def _base_url(value: str, label: str) -> str:
        base = value.strip().rstrip("/")
        parsed = urlparse(base)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(f"尚未配置有效的 {label} 地址")
        return base

    @staticmethod
    def _result(target: str, response: httpx.Response) -> ScanTriggerResult:
        accepted = 200 <= response.status_code < 300
        if not accepted:
            message = response.text.strip()[:500] or "扫描触发失败"
            raise RuntimeError(f"{target} 返回 {response.status_code}: {message}")
        return ScanTriggerResult(
            target=target,
            accepted=True,
            status_code=response.status_code,
            message="资料库扫描已触发",
        )
