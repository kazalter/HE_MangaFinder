from datetime import UTC, datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.models import Author, DiscoveryRun, ProviderScanRun
from app.modules.catalog.discovery_repository import DiscoveryRepository


def test_restart_marks_incomplete_scans_for_provider_retry():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)
    with Session(engine) as session:
        author = Author(name="author")
        session.add(author)
        session.flush()
        run = DiscoveryRun(author_id=author.id, status="running")
        session.add(run)
        session.flush()
        succeeded = ProviderScanRun(
            discovery_run_id=run.id,
            provider="wnacg",
            query_name="author",
            status="succeeded",
            finished_at=now - timedelta(minutes=2),
        )
        interrupted = ProviderScanRun(
            discovery_run_id=run.id,
            provider="nhentai",
            query_name="author",
            status="running",
        )
        session.add_all([succeeded, interrupted])
        session.commit()
        repository = DiscoveryRepository(session)
        assert repository.recover_interrupted(now) == 1
        assert run.status == "partial" and run.finished_at == now
        assert succeeded.status == "succeeded"
        assert interrupted.status == "failed" and interrupted.finished_at == now
        assert repository.due_provider_retries(
            now + timedelta(minutes=2), 1, 60, {"wnacg", "nhentai"}
        ) == {author.id: ["nhentai"]}
        assert repository.recover_interrupted(now) == 0
