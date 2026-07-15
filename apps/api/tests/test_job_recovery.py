from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.models import Job, JobStatus
from app.modules.jobs.repository import JobRepository


def test_running_jobs_are_requeued_after_restart() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        running = Job(
            kind="social_sync_account",
            payload={"account_id": 1},
            status=JobStatus.RUNNING,
            attempts=1,
            started_at=datetime.now(UTC),
        )
        succeeded = Job(kind="discover_author", status=JobStatus.SUCCEEDED)
        session.add_all([running, succeeded])
        session.commit()

        assert JobRepository(session).recover_interrupted() == 1
        session.refresh(running)
        session.refresh(succeeded)
        assert running.status == JobStatus.PENDING
        assert running.started_at is None
        assert running.attempts == 1
        assert "自动重新排队" in (running.error or "")
        assert succeeded.status == JobStatus.SUCCEEDED
