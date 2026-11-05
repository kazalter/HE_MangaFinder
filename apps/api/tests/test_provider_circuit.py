from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.models import Author, DiscoveryRun, ProviderScanRun
from app.modules.catalog.discovery_repository import DiscoveryRepository
from app.modules.catalog.service import DiscoveryService
from app.modules.dashboard.repository import DashboardRepository
from app.providers.errors import classify_provider_error
from app.providers.registry import ProviderRegistry


class Provider:
    name = "example"
    display_name = "Example"
    capabilities = set()

    def __init__(self):
        self.calls = 0
        self.error = "请求超时"

    async def discover_by_author(self, author_name, *, author_names=None):
        self.calls += 1
        if self.error:
            raise RuntimeError(self.error)
        return []

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_automatic_pause_probe_and_manual_override():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        author = Author(name="Artist")
        session.add(author)
        session.commit()
        provider = Provider()
        service = DiscoveryService(session, ProviderRegistry([provider]))
        for _ in range(3):
            with pytest.raises(RuntimeError):
                await service.discover_author(author.id)
        assert provider.calls == 3
        until = DiscoveryRepository(session).provider_pause_until(provider.name, 10, 120)
        assert until and until > datetime.now(UTC)
        result = await service.discover_author(author.id, trigger="scheduled")
        assert result["status"] == "skipped"
        assert result["skipped_providers"] == ["example"]
        assert provider.calls == 3

        # Manual refresh is never prevented by the automatic pause.
        provider.error = None
        result = await service.discover_author(author.id)
        assert result["status"] == "succeeded"
        assert provider.calls == 4
        assert DiscoveryRepository(session).provider_pause_until(provider.name, 10, 120) is None

        # A completed failure series becomes eligible for a single next scan
        # when its pause expires; a successful probe restores normal operation.
        old = datetime.now(UTC) - timedelta(days=2)
        for scan in session.scalars(select(ProviderScanRun)):
            scan.finished_at = old - timedelta(days=1)
        session.flush()
        for _ in range(3):
            run = DiscoveryRun(author_id=author.id, trigger="provider_retry", status="failed")
            session.add(run)
            session.flush()
            session.add(ProviderScanRun(
                discovery_run_id=run.id, provider=provider.name, query_name=author.name,
                status="failed", error="请求超时", started_at=old, finished_at=old,
            ))
        session.commit()
        pause_until = DiscoveryRepository(session).provider_pause_until(provider.name, 10, 120)
        assert pause_until is not None and pause_until < datetime.now(UTC)
        provider.error = "请求超时"
        result = await service.discover_author(
            author.id, trigger="provider_retry", provider_names={"example"}
        )
        assert result["status"] == "failed"
        pause_until = DiscoveryRepository(session).provider_pause_until(provider.name, 10, 120)
        assert pause_until is not None and pause_until > datetime.now(UTC)
        skipped = await service.discover_author(author.id, trigger="scheduled")
        assert skipped["status"] == "skipped"
        latest_failure = session.scalars(
            select(ProviderScanRun).where(ProviderScanRun.status == "failed")
            .order_by(ProviderScanRun.id.desc())
        ).first()
        latest_failure.finished_at = old
        session.commit()
        provider.error = None
        probe = await service.discover_author(
            author.id, trigger="provider_retry", provider_names={"example"}
        )
        assert probe["status"] == "succeeded"
        assert DiscoveryRepository(session).provider_pause_until(provider.name, 10, 120) is None
        health = DashboardRepository(session).dashboard(30).source_health[0]
        assert health.failure_categories["timeout"] == 7


@pytest.mark.parametrize(
    ("message", "category"),
    [
        ("请求超时", "timeout"), ("代理连接失败", "proxy"),
        ("HTTP 403", "forbidden"), ("HTTP 429", "rate_limit"),
        ("候选域名均不可用", "connection"), ("HTTP 503", "unavailable"),
        ("页面结构可能已变化", "parse"), ("未知问题", "other"),
    ],
)
def test_recorded_error_classification(message, category):
    assert classify_provider_error(message) == category



def test_scheduler_does_not_queue_paused_retries_and_resumes_probe(tmp_path, monkeypatch):
    from sqlalchemy.orm import sessionmaker

    from app.core.config import Settings
    from app.db.models import Job
    from app.modules.catalog import scheduler as scheduler_module
    from app.modules.catalog.scheduler import CatalogScheduler

    engine = create_engine(f"sqlite:///{tmp_path / 'scheduler.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(scheduler_module, "SessionLocal", factory)
    now = datetime.now(UTC)
    with factory() as session:
        author = Author(name="Artist", last_checked_at=now)
        session.add(author)
        session.flush()
        for _ in range(3):
            run = DiscoveryRun(
                author_id=author.id, trigger="provider_retry", status="failed", started_at=now
            )
            session.add(run)
            session.flush()
            session.add(ProviderScanRun(
                discovery_run_id=run.id, provider="example", query_name="Artist",
                status="failed", error="请求超时", started_at=now, finished_at=now,
            ))
        session.commit()
    settings = Settings(
        catalog_discovery_interval_minutes=1440,
        catalog_provider_retry_base_minutes=10,
        catalog_provider_retry_max_minutes=120,
    )
    scheduler = CatalogScheduler(settings, ProviderRegistry([Provider()]))
    assert scheduler.enqueue_due(now + timedelta(minutes=1)) == 0
    with factory() as session:
        assert list(session.scalars(select(Job))) == []
    # Per-author backoff may outlast the source pause; a due retry is the probe.
    assert scheduler.enqueue_due(now + timedelta(minutes=41)) == 1
    with factory() as session:
        job = session.scalar(select(Job))
        assert job.payload["trigger"] == "provider_retry"
        assert job.payload["providers"] == ["example"]
