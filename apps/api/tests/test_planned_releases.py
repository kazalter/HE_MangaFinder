from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.models import Author, PlannedRelease, ReleaseSignal
from app.modules.catalog.service import DiscoveryService
from app.modules.dashboard.repository import DashboardRepository
from app.modules.social.planned_releases import PlannedReleaseService
from app.providers.base import DiscoveredWork
from app.providers.registry import ProviderRegistry


class PlannedWorkProvider:
    name = "planned"
    display_name = "Planned"
    capabilities = set()

    async def discover_by_author(
        self, author_name: str, *, author_names: set[str] | None = None
    ) -> list[DiscoveredWork]:
        return [
            DiscoveredWork(
                external_id="release-1",
                title="星の本",
                source_url="https://example.test/release-1",
            )
        ]

    async def close(self) -> None:
        return None


def test_multiple_signals_share_one_planned_release_lifecycle() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        author = Author(name="作家")
        session.add(author)
        session.flush()
        first = ReleaseSignal(
            author_id=author.id,
            primary_post_id=1,
            cluster_key="first",
            kind="new_release",
            title="星の本",
            status="confirmed",
        )
        second = ReleaseSignal(
            author_id=author.id,
            primary_post_id=2,
            cluster_key="second",
            kind="preorder",
            title="星の本",
            event_code="C108",
        )
        session.add_all([first, second])
        session.flush()

        service = PlannedReleaseService(session)
        planned = service.absorb_signal(first)
        same = service.absorb_signal(second)

        assert planned is not None and same is not None
        assert same.id == planned.id
        assert first.planned_release_id == second.planned_release_id
        assert same.event_code == "C108"
        assert len(list(session.scalars(select(PlannedRelease)))) == 1


@pytest.mark.asyncio
async def test_catalog_discovery_suggests_exact_planned_release_match() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        author = Author(name="作家")
        session.add(author)
        session.flush()
        planned = PlannedRelease(
            author_id=author.id,
            title="星の本",
            normalized_title="星の本",
            release_date=datetime.now(UTC) + timedelta(days=7),
        )
        session.add(planned)
        session.commit()

        await DiscoveryService(
            session, ProviderRegistry([PlannedWorkProvider()])
        ).discover_author(author.id)

        session.refresh(planned)
        assert planned.status == "discovered"
        assert planned.suggested_group_id is not None
        dashboard = DashboardRepository(session).dashboard(30)
        assert dashboard.upcoming[0].id == planned.id
        assert dashboard.upcoming[0].linked_group_id is None
