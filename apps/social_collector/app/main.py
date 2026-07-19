import asyncio
import os
import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    async_playwright,
)
from playwright.async_api import (
    Error as PlaywrightError,
)

from app.graphql import GraphQLTransientError, GraphQLUnavailable, WebGraphQLCollector

TOKEN = os.getenv("SOCIAL_COLLECTOR_TOKEN", "")
STATE_SOURCE = Path(os.getenv("SOCIAL_COLLECTOR_STORAGE_STATE", "/session/storage-state.json"))
STATE_RUNTIME = Path(os.getenv("SOCIAL_COLLECTOR_RUNTIME_STATE", "/session/runtime-state.json"))
HEADLESS = os.getenv("SOCIAL_COLLECTOR_HEADLESS", "true").lower() != "false"
PROXY_URL = os.getenv("SOCIAL_COLLECTOR_PROXY_URL", "").strip()
CHROMIUM_EXECUTABLE = os.getenv("SOCIAL_COLLECTOR_CHROMIUM_EXECUTABLE", "/usr/bin/chromium").strip()
POST_RE = re.compile(r"/status/(\d+)")
HANDLE_RE = re.compile(r"^/([A-Za-z0-9_]{1,15})$")


class BrowserCollector:
    def __init__(self) -> None:
        self.playwright: Any = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.lock = asyncio.Lock()

    async def start(self) -> None:
        self.playwright = await async_playwright().start()
        launch_options: dict[str, Any] = {
            "headless": HEADLESS,
            "executable_path": CHROMIUM_EXECUTABLE,
            "args": ["--no-sandbox", "--disable-dev-shm-usage"],
        }
        if PROXY_URL:
            launch_options["proxy"] = {"server": PROXY_URL}
        self.browser = await self.playwright.chromium.launch(**launch_options)
        self.context = await self._new_context()

    async def _new_context(self) -> BrowserContext:
        if not self.browser:
            raise RuntimeError("浏览器尚未启动")
        state = STATE_RUNTIME if STATE_RUNTIME.exists() else STATE_SOURCE
        context_options: dict[str, Any] = {
            "locale": "ja-JP",
            "timezone_id": "Asia/Tokyo",
            "viewport": {"width": 1440, "height": 1100},
            "user_agent": os.getenv("SOCIAL_COLLECTOR_USER_AGENT", "").strip()
            or (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
        }
        if state.exists():
            context_options["storage_state"] = str(state)
        return await self.browser.new_context(**context_options)

    async def reload_session(self) -> None:
        async with self.lock:
            if self.context:
                await self.context.close()
            STATE_RUNTIME.unlink(missing_ok=True)
            self.context = await self._new_context()

    async def check_session(self) -> bool:
        async with self.lock:
            page = await self.page()
            try:
                await self.navigate(page, "https://x.com/home")
                await page.wait_for_timeout(1200)
                await self.ensure_access(page)
                return "/home" in page.url
            finally:
                await page.close()

    async def close(self) -> None:
        if self.context:
            try:
                STATE_RUNTIME.parent.mkdir(parents=True, exist_ok=True)
                await self.context.storage_state(path=str(STATE_RUNTIME))
            finally:
                await self.context.close()
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()

    async def page(self) -> Page:
        if not self.context:
            raise RuntimeError("浏览器尚未启动")
        return await self.context.new_page()

    async def ensure_access(self, page: Page) -> None:
        if "/login" in page.url or "/i/flow/login" in page.url:
            raise HTTPException(status_code=503, detail="X 登录会话已失效，请重新导入会话")
        body = (await page.locator("body").inner_text()).casefold()
        if "something went wrong" in body or "問題が発生しました" in body:
            raise HTTPException(status_code=503, detail="X 页面加载失败或触发访问限制")

    async def navigate(self, page: Page, url: str) -> None:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except PlaywrightError as exc:
            message = str(exc).casefold()
            if "err_timed_out" in message or "timeout" in message:
                detail = "连接 X 超时，请检查动态雷达的代理地址和代理服务"
            elif "err_proxy_connection_failed" in message:
                detail = "无法连接动态雷达代理，请检查代理地址和端口"
            else:
                detail = "无法打开 X 页面，请检查网络、代理或 X 的访问状态"
            raise HTTPException(status_code=503, detail=detail) from exc

    async def suggest(self, query: str) -> list[dict[str, Any]]:
        async with self.lock:
            page = await self.page()
            try:
                await self.navigate(
                    page,
                    f"https://x.com/search?q={quote(query)}&src=typed_query&f=user",
                )
                await page.wait_for_timeout(2500)
                await self.ensure_access(page)
                cells = page.locator('[data-testid="UserCell"]')
                results: list[dict[str, Any]] = []
                for index in range(min(await cells.count(), 12)):
                    cell = cells.nth(index)
                    text = (await cell.inner_text()).strip()
                    handle = None
                    profile_url = None
                    for anchor in await cell.locator("a[href]").all():
                        href = await anchor.get_attribute("href")
                        match = HANDLE_RE.match(href or "")
                        if match:
                            handle = match.group(1)
                            profile_url = f"https://x.com/{handle}"
                            break
                    if not handle or any(
                        item["handle"].casefold() == handle.casefold() for item in results
                    ):
                        continue
                    avatar = cell.locator('img[src*="profile_images"]')
                    avatar_url = (
                        await avatar.first.get_attribute("src") if await avatar.count() else None
                    )
                    display_name = next(
                        (line for line in text.splitlines() if not line.startswith("@")), handle
                    )
                    exact = query.casefold() in text.casefold()
                    results.append(
                        {
                            "handle": handle,
                            "display_name": display_name,
                            "profile_url": profile_url,
                            "avatar_url": avatar_url,
                            "score": 0.88 if exact else 0.55,
                            "evidence": [
                                "X 用户搜索命中作者名" if exact else "X 用户搜索候选；需要人工核对"
                            ],
                        }
                    )
                return results
            finally:
                await page.close()

    async def posts(self, handle: str, since_id: str | None, limit: int) -> list[dict[str, Any]]:
        async with self.lock:
            page = await self.page()
            try:
                await self.navigate(page, f"https://x.com/{handle}")
                await page.wait_for_timeout(2500)
                await self.ensure_access(page)
                found: dict[str, dict[str, Any]] = {}
                stalled = 0
                for _ in range(24):
                    articles = page.locator('article[data-testid="tweet"]')
                    before = len(found)
                    for index in range(await articles.count()):
                        parsed = await self._parse_article(articles.nth(index), handle)
                        if not parsed:
                            continue
                        if since_id and parsed["id"] == since_id:
                            return sorted(
                                found.values(), key=lambda item: item["posted_at"], reverse=True
                            )
                        found[parsed["id"]] = parsed
                        if len(found) >= limit:
                            return sorted(
                                found.values(), key=lambda item: item["posted_at"], reverse=True
                            )
                    stalled = stalled + 1 if len(found) == before else 0
                    if stalled >= 3:
                        break
                    await page.mouse.wheel(0, 3000)
                    await page.wait_for_timeout(1100)
                return sorted(found.values(), key=lambda item: item["posted_at"], reverse=True)
            finally:
                STATE_RUNTIME.parent.mkdir(parents=True, exist_ok=True)
                if self.context:
                    await self.context.storage_state(path=str(STATE_RUNTIME))
                await page.close()

    async def post_status(self, handle: str, post_id: str) -> dict[str, str | None]:
        async with self.lock:
            page = await self.page()
            try:
                await self.navigate(page, f"https://x.com/{handle}/status/{post_id}")
                await page.wait_for_timeout(1800)
                await self.ensure_access(page)
                for article in await page.locator('article[data-testid="tweet"]').all():
                    for anchor in await article.locator('a[href*="/status/"]').all():
                        href = await anchor.get_attribute("href")
                        match = POST_RE.search(href or "")
                        if match and match.group(1) == post_id:
                            return {"status": "available", "reason": None}
                body = (await page.locator("body").inner_text()).strip()
                folded = body.casefold()
                deleted_words = (
                    "deleted by the post author",
                    "post was deleted",
                    "tweet was deleted",
                    "削除されました",
                    "已被删除",
                    "已刪除",
                )
                status = (
                    "deleted"
                    if any(word in folded for word in deleted_words)
                    else "unavailable"
                )
                return {"status": status, "reason": body[:500] or "X 页面没有返回帖子"}
            finally:
                await page.close()

    async def _parse_article(self, article: Any, handle: str) -> dict[str, Any] | None:
        time_locator = article.locator("time")
        if not await time_locator.count():
            return None
        time_node = time_locator.first
        posted = await time_node.get_attribute("datetime")
        href = await time_node.locator("xpath=..").get_attribute("href")
        match = POST_RE.search(href or "")
        if not posted or not match:
            return None
        post_id = match.group(1)
        tweet_url = f"https://x.com/{handle}/status/{post_id}"
        author_handle = handle
        user_links = article.locator('[data-testid="User-Name"] a[href^="/"]')
        for user_link in await user_links.all():
            user_href = await user_link.get_attribute("href")
            user_match = HANDLE_RE.match(user_href or "")
            if user_match:
                author_handle = user_match.group(1)
                break
        text_nodes = article.locator('[data-testid="tweetText"]')
        text = await text_nodes.first.inner_text() if await text_nodes.count() else ""
        social = article.locator('[data-testid="socialContext"]')
        social_text = await social.first.inner_text() if await social.count() else ""
        article_text = await article.inner_text()
        post_type = "original"
        if any(term in social_text.casefold() for term in ("reposted", "リポスト", "转帖", "轉發")):
            post_type = "retweet"
        elif author_handle.casefold() != handle.casefold():
            post_type = "retweet"
        elif any(
            term in article_text.casefold() for term in ("replying to", "返信先", "回复", "回覆")
        ):
            post_type = "reply"
        card = article.locator('[data-testid="card.wrapper"]')
        if post_type == "original" and await time_locator.count() > 1:
            post_type = "quote"
        media: list[dict[str, str]] = []
        for image in await article.locator('img[src*="pbs.twimg.com/media"]').all():
            src = await image.get_attribute("src")
            alt = await image.get_attribute("alt")
            if src and not any(item["url"] == src for item in media):
                media.append({"type": "image", "url": src, "alt_text": alt or ""})
        links: list[str] = []
        for anchor in await article.locator("a[href]").all():
            target = (
                await anchor.get_attribute("data-expanded-url")
                or await anchor.get_attribute("title")
                or await anchor.get_attribute("href")
            )
            if target and target.startswith("http") and target not in links:
                links.append(target)
        quoted_text = await card.first.inner_text() if await card.count() else None
        related_ids: list[str] = []
        for anchor in await article.locator('a[href*="/status/"]').all():
            related_href = await anchor.get_attribute("href")
            related_match = POST_RE.search(related_href or "")
            if related_match and related_match.group(1) != post_id:
                related_ids.append(related_match.group(1))
        related_id = related_ids[0] if related_ids else None
        return {
            "id": post_id,
            "text": text,
            "url": tweet_url,
            "post_type": post_type,
            "posted_at": datetime.fromisoformat(posted.replace("Z", "+00:00"))
            .astimezone(UTC)
            .isoformat(),
            "conversation_id": related_id if post_type == "reply" else post_id,
            "replied_to_post_id": related_id if post_type == "reply" else None,
            "quoted_post_id": related_id if post_type == "quote" else None,
            "media": media,
            "links": links,
            "raw": {
                "author_handle": author_handle,
                "social_context": social_text,
                "quoted_text": quoted_text,
            },
        }


collector = BrowserCollector()
graphql_collector = WebGraphQLCollector(
    STATE_SOURCE,
    PROXY_URL,
    os.getenv("SOCIAL_COLLECTOR_USER_AGENT", "").strip()
    or (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
    ),
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await collector.start()
    yield
    await collector.close()


app = FastAPI(title="MangaFinder Social Collector", lifespan=lifespan)


def authorize(authorization: Annotated[str | None, Header()] = None) -> None:
    if TOKEN and authorization != f"Bearer {TOKEN}":
        raise HTTPException(status_code=401, detail="采集器令牌无效")


@app.get("/health")
def health(_: Annotated[None, Depends(authorize)]) -> dict[str, object]:
    return {
        "status": "ok",
        "session_present": STATE_SOURCE.exists() or STATE_RUNTIME.exists(),
        "graphql_session_present": graphql_collector.available,
        "primary_provider": "x_web_graphql" if graphql_collector.available else "browser",
        "session_valid": None,
        "headless": HEADLESS,
    }


@app.post("/session/reload", dependencies=[Depends(authorize)])
async def reload_session() -> dict[str, bool]:
    await collector.reload_session()
    return {"reloaded": True}


@app.get("/session/check", dependencies=[Depends(authorize)])
async def check_session() -> dict[str, object]:
    if graphql_collector.available:
        try:
            user = await asyncio.to_thread(graphql_collector.lookup_user, "X")
            if user:
                return {"valid": True, "provider": "x_web_graphql"}
        except GraphQLTransientError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except GraphQLUnavailable:
            pass
    valid = await collector.check_session()
    return {
        "valid": valid,
        "provider": "browser",
        "detail": None if valid else "X 登录会话无效",
    }


@app.get("/accounts/suggest", dependencies=[Depends(authorize)])
async def suggest(q: Annotated[str, Query(min_length=1, max_length=200)]) -> list[dict[str, Any]]:
    if graphql_collector.available:
        try:
            exact = await asyncio.to_thread(graphql_collector.suggestions, q)
            if exact:
                return exact
        except (GraphQLUnavailable, GraphQLTransientError):
            pass
    return await collector.suggest(q)


@app.get("/accounts/{handle}", dependencies=[Depends(authorize)])
async def account_profile(handle: str) -> dict[str, Any] | None:
    clean = handle.removeprefix("@")
    if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", clean):
        raise HTTPException(status_code=422, detail="X 账号格式无效")
    if graphql_collector.available:
        try:
            user = await asyncio.to_thread(graphql_collector.lookup_user, clean)
            if not user:
                return None
            return {
                **user,
                "profile_url": f"https://x.com/{user['handle']}",
            }
        except GraphQLUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except GraphQLTransientError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    suggestions = await collector.suggest(clean)
    return next(
        (
            item
            for item in suggestions
            if str(item.get("handle", "")).casefold() == clean.casefold()
        ),
        None,
    )


@app.get("/accounts/{handle}/posts", dependencies=[Depends(authorize)])
async def posts(
    handle: str,
    since_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> list[dict[str, Any]]:
    clean = handle.removeprefix("@")
    if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", clean):
        raise HTTPException(status_code=422, detail="X 账号格式无效")
    if graphql_collector.available:
        try:
            return await asyncio.to_thread(graphql_collector.posts, clean, since_id, limit)
        except GraphQLUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except GraphQLTransientError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    return await collector.posts(clean, since_id, limit)


@app.get("/posts/{post_id}/status", dependencies=[Depends(authorize)])
async def post_status(post_id: str, handle: str) -> dict[str, str | None]:
    clean = handle.removeprefix("@")
    if not post_id.isdigit() or not re.fullmatch(r"[A-Za-z0-9_]{1,15}", clean):
        raise HTTPException(status_code=422, detail="X 帖子参数无效")
    if graphql_collector.available:
        try:
            return await asyncio.to_thread(graphql_collector.post_status, post_id)
        except GraphQLUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except GraphQLTransientError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    return await collector.post_status(clean, post_id)
