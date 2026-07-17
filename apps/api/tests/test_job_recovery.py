from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.db.models import Author, Job, JobStatus
from app.modules.jobs import worker as worker_module
from app.modules.jobs.repository import JobRepository
from app.modules.jobs.worker import JobWorker
from app.providers.registry import ProviderRegistry


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


@pytest.mark.asyncio
async def test_failed_job_rolls_back_uncommitted_business_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        author = Author(name="原作者")
        session.add(author)
        session.flush()
        job = JobRepository(session).enqueue_discovery(author.id)
        job_id = job.id
        session.commit()

    async def failing_discovery(service, _author_id):
        service.session.add(Author(name="不应提交的半成品"))
        service.session.flush()
        raise RuntimeError("发现任务中途失败")

    monkeypatch.setattr(worker_module, "SessionLocal", factory)
    monkeypatch.setattr(
        worker_module.DiscoveryService, "discover_author", failing_discovery
    )
    worker = JobWorker(Settings(max_job_attempts=1), ProviderRegistry([]))

    assert await worker.run_once() is True

    with factory() as session:
        failed = session.get(Job, job_id)
        assert failed is not None
        assert failed.status == JobStatus.FAILED
        assert failed.error == "发现任务中途失败"
        names = set(session.scalars(select(Author.name)))
        assert names == {"原作者"}
