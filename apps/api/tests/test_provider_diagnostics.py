import json

import httpx
import pytest

from app.providers.errors import ProviderError
from app.providers.nhentai import NhentaiProvider
from app.providers.wnacg import WnacgProvider


@pytest.mark.parametrize(
    ("error_type", "message"),
    [
        (httpx.ReadTimeout, "请求超时"),
        (httpx.ProxyError, "代理连接失败"),
        (httpx.ConnectError, "连接失败"),
    ],
)
async def test_nhentai_reports_transport_failure(error_type, message):
    def handler(request):
        raise error_type("private details", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = NhentaiProvider(user_agent="test", client=client)
        with pytest.raises(ProviderError, match=message) as error:
            await provider._request_page("/search/")
        assert "private details" not in str(error.value)


async def test_wnacg_reports_each_mirror_without_credentials():
    def handler(request):
        if request.url.host == "first.example":
            return httpx.Response(429)
        raise httpx.ReadTimeout("hidden", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = WnacgProvider(
            user_agent="test",
            base_urls=["https://first.example", "https://second.example"],
            client=client,
        )
        with pytest.raises(ProviderError) as error:
            await provider._request_site("/search/")
        assert "first.example: HTTP 429" in str(error.value)
        assert "second.example: 请求超时" in str(error.value)
        assert "hidden" not in str(error.value)


def test_nhentai_distinguishes_empty_results_from_changed_page():
    body = json.dumps({"result": [], "num_pages": 1})
    script = json.dumps({"body": body})
    assert NhentaiProvider.parse_listing(
        f'<script type="application/json">{script}</script>', "x"
    ) == ([], 1)
    with pytest.raises(ProviderError, match="页面结构"):
        NhentaiProvider.parse_listing("<html>unexpected page</html>", "x")
