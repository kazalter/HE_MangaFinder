import json

import httpx

from app.core.config import Settings
from app.modules.social.collector import XBrowserCollector


async def test_xbrowser_collector_context_reuses_client_and_sets_budget_header() -> None:
    seen_headers: list[str | None] = []
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        seen_headers.append(request.headers.get("X-Request-Budget-Seconds"))
        if request.url.path == "/accounts/artist":
            return httpx.Response(
                200,
                json={
                    "id": "42",
                    "handle": "artist",
                    "display_name": "Artist",
                    "profile_url": "https://x.com/artist",
                },
            )
        return httpx.Response(
            200,
            json={"posts": [], "complete": True, "next_cursor": None},
        )

    settings = Settings(
        social_collector_base_url="http://collector.test",
        social_collector_batch_budget_seconds=50.0,
        social_collector_request_timeout_seconds=80.0,
    )
    async with XBrowserCollector(settings) as collector:
        assert collector._owned_client is not None
        timeout = collector._default_timeout()
        assert timeout.read is not None and timeout.read >= 70.0
        # Swap underlying transport on the single owned client to verify reuse across calls
        collector._owned_client._transport = httpx.MockTransport(handler)
        profile = await collector.profile("artist")
        batch = await collector.posts_batch("artist", None, 20)

    assert collector._owned_client is None
    assert calls == 2
    assert profile is not None and profile.handle == "artist"
    assert batch.complete is True
    assert json.loads(seen_headers[1] or "0") == 50.0
