import httpx
import pytest

from app.core.config import Settings
from app.modules.social.collector import XBrowserCollector


@pytest.mark.asyncio
async def test_collector_exposes_structured_error_detail() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            json={"detail": "连接 X 超时，请检查动态雷达的代理地址和代理服务"},
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        collector = XBrowserCollector(
            Settings(social_collector_base_url="http://collector:8010"), client
        )
        with pytest.raises(RuntimeError, match="连接 X 超时"):
            await collector.suggestions("作者")


@pytest.mark.asyncio
async def test_collector_handles_non_json_error_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Internal Server Error", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        collector = XBrowserCollector(
            Settings(social_collector_base_url="http://collector:8010"), client
        )
        with pytest.raises(RuntimeError, match="Internal Server Error"):
            await collector.suggestions("作者")


@pytest.mark.asyncio
async def test_collector_reads_account_profile() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/accounts/creator"
        return httpx.Response(
            200,
            json={
                "id": "42",
                "handle": "creator",
                "display_name": "Creator",
                "profile_url": "https://x.com/creator",
                "avatar_url": "https://pbs.twimg.com/profile_images/42/avatar.jpg",
            },
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        profile = await XBrowserCollector(
            Settings(social_collector_base_url="http://collector:8010"), client
        ).profile("creator")

    assert profile is not None
    assert profile.id == "42"
    assert profile.avatar_url == "https://pbs.twimg.com/profile_images/42/avatar.jpg"
