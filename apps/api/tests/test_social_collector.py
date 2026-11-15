from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.db.models import Author, Job, SocialAccount
from app.modules.social.collector import CollectorUnavailableError, XBrowserCollector
from app.modules.social.scheduler import SocialScheduler
from app.modules.social.service import SocialSyncService


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


@pytest.mark.asyncio
async def test_collector_raises_unavailable_on_connect_error_and_timeout() -> None:
    def connect_error_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(connect_error_handler)) as client:
        collector = XBrowserCollector(
            Settings(social_collector_base_url="http://collector:8010"), client
        )
        with pytest.raises(CollectorUnavailableError, match="无法连接 X 采集器"):
            await collector.health()

    def connect_timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("connect timed out", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(connect_timeout_handler)) as client:
        collector = XBrowserCollector(
            Settings(social_collector_base_url="http://collector:8010"), client
        )
        with pytest.raises(CollectorUnavailableError, match="无法连接 X 采集器"):
            await collector.profile("creator")


@pytest.mark.asyncio
async def test_sync_account_uses_short_backoff_when_collector_unavailable(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'social.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        author = Author(name="测试作者")
        session.add(author)
        session.flush()
        account = SocialAccount(author_id=author.id, handle="creator", status="confirmed")
        session.add(account)
        session.commit()
        account_id = account.id

    posts_called = False

    async def unreachable_profile(*args, **kwargs):
        raise CollectorUnavailableError("collector starting")

    async def unexpected_posts(*args, **kwargs):
        nonlocal posts_called
        posts_called = True
        raise AssertionError("posts_batch should not run after profile connect error")

    monkeypatch.setattr(
        "app.modules.social.service.XBrowserCollector.profile", unreachable_profile
    )
    monkeypatch.setattr(
        "app.modules.social.service.XBrowserCollector.posts_batch", unexpected_posts
    )

    before = datetime.now(UTC)
    with factory() as session:
        service = SocialSyncService(session, Settings(social_media_dir=tmp_path))
        with pytest.raises(CollectorUnavailableError):
            await service.sync_account(account_id)

    with factory() as session:
        updated = session.get(SocialAccount, account_id)
        assert updated is not None
        assert posts_called is False
        assert updated.sync_error == "collector starting"
        assert updated.next_sync_at is not None
        delta = updated.next_sync_at.replace(tzinfo=UTC) - before
        assert timedelta(minutes=1) <= delta <= timedelta(minutes=3)


@pytest.mark.asyncio
async def test_social_scheduler_defers_account_sync_until_collector_healthy(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'scheduler.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        author = Author(name="测试作者")
        session.add(author)
        session.flush()
        account = SocialAccount(
            author_id=author.id,
            handle="creator",
            status="confirmed",
            next_sync_at=datetime.now(UTC) - timedelta(minutes=5),
        )
        session.add(account)
        session.commit()

    monkeypatch.setattr("app.modules.social.scheduler.SessionLocal", factory)
    settings = Settings(
        social_ocr_enabled=False,
        social_agent_enabled=False,
        social_daily_digest_enabled=False,
    )
    scheduler = SocialScheduler(settings)

    async def unhealthy(_self):
        raise CollectorUnavailableError("collector booting")

    monkeypatch.setattr(XBrowserCollector, "health", unhealthy)
    ready = await scheduler._collector_ready()
    assert ready is False
    scheduler.enqueue_due(include_account_syncs=ready)
    with factory() as session:
        assert session.scalar(select(Job).where(Job.kind == "social_sync_account")) is None

    async def healthy(_self):
        return {"status": "ok"}

    monkeypatch.setattr(XBrowserCollector, "health", healthy)
    ready = await scheduler._collector_ready()
    assert ready is True
    scheduler.enqueue_due(include_account_syncs=ready)
    with factory() as session:
        assert session.scalar(select(Job).where(Job.kind == "social_sync_account")) is not None
