from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.models import Author, CatalogEvent, DiscoveryRun, ProviderScanRun
from app.modules.catalog.discovery_repository import DiscoveryRepository
from app.modules.catalog.service import DiscoveryService
from app.modules.dashboard.repository import DashboardRepository
from app.providers.base import DiscoveredWork
from app.providers.registry import ProviderRegistry


class FakeProvider:
    name = "fake"
    display_name = "Fake"
    capabilities = set()

    def __init__(self, *, error: str | None = None) -> None:
        self.error = error

    async def discover_by_author(self, author_name: str) -> list[DiscoveredWork]:
        if self.error:
            raise RuntimeError(self.error)
        return [
            DiscoveredWork(
                external_id="work-1",
                title=f"{author_name} Work",
                source_url="https://example.test/work-1",
                language="ja",
            )
        ]

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_discovery_records_provider_result_and_inbox_event() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        author = Author(name="Artist")
        session.add(author)
        session.commit()

        result = await DiscoveryService(
            session, ProviderRegistry([FakeProvider()])
        ).discover_author(author.id, trigger="scheduled")

        assert result["status"] == "succeeded"
        assert result["new"] == 1
        run = session.scalar(select(DiscoveryRun))
        provider_run = session.scalar(select(ProviderScanRun))
        event = session.scalar(select(CatalogEvent))
        assert run is not None and run.trigger == "scheduled" and run.new_count == 1
        assert provider_run is not None and provider_run.status == "succeeded"
        assert event is not None and event.event_type == "work_discovered"

        repeated = await DiscoveryService(
            session, ProviderRegistry([FakeProvider()])
        ).discover_author(author.id)
        assert repeated["new"] == 0
        assert len(list(session.scalars(select(CatalogEvent)))) == 1


@pytest.mark.asyncio
async def test_partial_provider_failure_is_visible_without_failing_whole_scan() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        author = Author(name="Artist")
        session.add(author)
        session.commit()
        failing = FakeProvider(error="source unavailable")
        failing.name = "broken"
        failing.display_name = "Broken"

        result = await DiscoveryService(
            session, ProviderRegistry([FakeProvider(), failing])
        ).discover_author(author.id)

        assert result["status"] == "partial"
        run = session.scalars(select(DiscoveryRun).order_by(DiscoveryRun.id.desc())).first()
        assert run is not None and "source unavailable" in (run.error or "")
        broken = session.scalar(
            select(ProviderScanRun).where(ProviderScanRun.provider == "broken")
        )
        assert broken is not None and broken.status == "failed"


@pytest.mark.asyncio
async def test_all_provider_failures_persist_a_failed_scan() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        author = Author(name="Artist")
        session.add(author)
        session.commit()

        with pytest.raises(RuntimeError, match="offline"):
            await DiscoveryService(
                session, ProviderRegistry([FakeProvider(error="offline")])
            ).discover_author(author.id)

        run = session.scalar(select(DiscoveryRun))
        assert run is not None and run.status == "failed"
        assert run.finished_at is not None


def test_due_author_selection_and_dashboard_inbox_state() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)
    with Session(engine, expire_on_commit=False) as session:
        stale = Author(name="Stale", last_checked_at=now - timedelta(days=2))
        fresh = Author(name="Fresh", last_checked_at=now)
        session.add_all([stale, fresh])
        session.commit()

        due = DiscoveryRepository(session).due_authors(now, 1440)
        assert [author.name for author in due] == ["Stale"]

        dashboard = DashboardRepository(session).dashboard(30)
        assert dashboard.summary.authors == 2
        assert dashboard.summary.unread_discoveries == 0
