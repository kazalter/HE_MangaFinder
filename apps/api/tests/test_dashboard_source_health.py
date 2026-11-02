from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.models import Author, DiscoveryRun, ProviderScanRun
from app.modules.dashboard.repository import DashboardRepository


def test_source_health_tracks_latest_result_for_each_author():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)
    with Session(engine) as session:
        authors = [Author(name="first"), Author(name="second")]
        session.add_all(authors)
        session.flush()

        def scan(author, status, minutes_ago, error=None):
            when = now - timedelta(minutes=minutes_ago)
            run = DiscoveryRun(author_id=author.id, started_at=when)
            session.add(run)
            session.flush()
            session.add(
                ProviderScanRun(
                    discovery_run_id=run.id,
                    provider="wnacg",
                    query_name=author.name,
                    status=status,
                    started_at=when,
                    finished_at=when,
                    error=error,
                )
            )
            session.flush()

        scan(authors[0], "failed", 5, "old error")
        scan(authors[0], "succeeded", 3)
        scan(authors[1], "failed", 4, "still failing")
        health = DashboardRepository(session)._source_health(now - timedelta(days=1))[0]
        assert health.failed == 2
        assert health.current_checked_authors == 2
        assert health.current_failed_authors == 1
        assert health.last_error == "still failing"

        scan(authors[1], "succeeded", 1)
        health = DashboardRepository(session)._source_health(now - timedelta(days=1))[0]
        assert health.failed == 2
        assert health.current_failed_authors == 0
        assert health.last_error is None
