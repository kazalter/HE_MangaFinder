import asyncio

import app.main as main
from app.main import BrowserCollector


def test_browser_batches_resume_and_reach_watermark(tmp_path, monkeypatch):
    collector = BrowserCollector()
    pages = [["5", "4"], ["3", "2"], ["1"]]
    calls = []

    class Articles:
        def __init__(self, page):
            self.page = page

        async def count(self):
            return len(pages[self.page.index])

        def nth(self, index):
            return pages[self.page.index][index]

    class Page:
        def __init__(self):
            self.index = 0
            self.mouse = self

        def locator(self, selector):
            return Articles(self)

        async def wait_for_timeout(self, timeout):
            pass

        async def wheel(self, x, y):
            self.index = min(self.index + 1, len(pages) - 1)

        async def close(self):
            pass

    async def get_page():
        return Page()

    async def navigate(page, url):
        pass

    async def ensure_access(page):
        pass

    async def parse(post_id, handle):
        calls.append(post_id)
        return {"id": post_id, "posted_at": "2026-11-07T12:00:00+00:00"}

    monkeypatch.setattr(main, "STATE_RUNTIME", tmp_path / "runtime.json")
    monkeypatch.setattr(collector, "page", get_page)
    monkeypatch.setattr(collector, "navigate", navigate)
    monkeypatch.setattr(collector, "ensure_access", ensure_access)
    monkeypatch.setattr(collector, "_parse_article", parse)
    first = asyncio.run(collector.posts_batch("creator", "1", 2))
    assert [post["id"] for post in first["posts"]] == ["5", "4"]
    assert first["next_cursor"] == "browser:4"
    second = asyncio.run(collector.posts_batch("creator", "1", 2, "4"))
    assert [post["id"] for post in second["posts"]] == ["3", "2"]
    assert second["next_cursor"] == "browser:2"
    last = asyncio.run(collector.posts_batch("creator", "1", 2, "2"))
    assert last["posts"] == [] and last["complete"] is True
