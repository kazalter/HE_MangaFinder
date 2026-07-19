from datetime import datetime
from typing import Any

import httpx
from pydantic import BaseModel, Field

from app.core.config import Settings
from app.modules.social.schemas import SocialAccountSuggestion


class CollectorPost(BaseModel):
    id: str
    text: str = ""
    url: str
    post_type: str = "original"
    posted_at: datetime
    conversation_id: str | None = None
    replied_to_post_id: str | None = None
    quoted_post_id: str | None = None
    media: list[dict[str, Any]] = Field(default_factory=list)
    links: list[str] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)


class CollectorProfile(BaseModel):
    id: str = ""
    handle: str
    display_name: str | None = None
    profile_url: str | None = None
    avatar_url: str | None = None


class XBrowserCollector:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = client

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.settings.social_collector_token:
            headers["Authorization"] = f"Bearer {self.settings.social_collector_token}"
        return headers

    @staticmethod
    def _response_error_detail(response: httpx.Response) -> str:
        try:
            body = response.json()
        except ValueError:
            return response.text.strip()[:500] or "采集器没有返回错误说明"
        if isinstance(body, dict) and isinstance(body.get("detail"), str):
            return body["detail"][:500]
        return response.text.strip()[:500] or "采集器没有返回错误说明"

    async def _request(self, method: str, path: str, **kwargs: object) -> Any:
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=90, follow_redirects=True)
        try:
            response = await client.request(
                method,
                f"{self.settings.social_collector_base_url.rstrip('/')}{path}",
                headers=self._headers(),
                **kwargs,
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            detail = self._response_error_detail(exc.response)
            raise RuntimeError(f"X 采集器暂时不可用：{detail}") from exc
        except httpx.ConnectError as exc:
            raise RuntimeError(
                "无法连接 X 采集器：采集器尚未启动、正在重启，或容器网络不可达"
            ) from exc
        except httpx.TimeoutException as exc:
            raise RuntimeError("连接 X 采集器超时，请检查采集器运行状态和网络") from exc
        except httpx.HTTPError as exc:
            raise RuntimeError(f"X 采集器网络请求失败：{exc}") from exc
        finally:
            if owns_client:
                await client.aclose()

    async def suggestions(self, query: str) -> list[SocialAccountSuggestion]:
        body = await self._request("GET", "/accounts/suggest", params={"q": query})
        return [SocialAccountSuggestion.model_validate(item) for item in body]

    async def profile(self, handle: str) -> CollectorProfile | None:
        body = await self._request("GET", f"/accounts/{handle}")
        return CollectorProfile.model_validate(body) if isinstance(body, dict) else None

    async def health(self) -> dict[str, Any]:
        body = await self._request("GET", "/health", timeout=5)
        return body if isinstance(body, dict) else {}

    async def check_session(self) -> dict[str, Any]:
        body = await self._request("GET", "/session/check")
        return body if isinstance(body, dict) else {}

    async def reload_session(self) -> dict[str, Any]:
        body = await self._request("POST", "/session/reload")
        return body if isinstance(body, dict) else {}

    async def posts(
        self, handle: str, since_id: str | None, limit: int
    ) -> list[CollectorPost]:
        params: dict[str, str | int] = {"limit": limit}
        if since_id:
            params["since_id"] = since_id
        body = await self._request(
            "GET", f"/accounts/{handle}/posts", params=params
        )
        return [CollectorPost.model_validate(item) for item in body]

    async def post_status(self, handle: str, post_id: str) -> dict[str, Any]:
        body = await self._request(
            "GET", f"/posts/{post_id}/status", params={"handle": handle}
        )
        return body if isinstance(body, dict) else {}
