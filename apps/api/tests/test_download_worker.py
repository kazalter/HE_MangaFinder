import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.db.models import Job, JobStatus
from app.modules.catalog.downloads import DownloadVerification
from app.modules.jobs import worker as worker_module
from app.modules.jobs.repository import DELIVER_NOTIFICATIONS, DOWNLOAD_CHAPTER
from app.modules.jobs.worker import DownloadWorker, JobWorker
from app.providers.registry import ProviderRegistry


@pytest.mark.asyncio
async def test_long_download_does_not_block_other_jobs_or_event_loop(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'jobs.db'}",
        connect_args={"check_same_thread": False, "timeout": 0.05},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(worker_module, "SessionLocal", factory)
    with factory() as session:
        download = Job(
            kind=DOWNLOAD_CHAPTER, payload={"work_id": 1, "provider": "test", "chapter_id": "1"}
        )
        notification = Job(kind=DELIVER_NOTIFICATIONS, payload={})
        session.add_all([download, notification])
        session.commit()
        download_id, notification_id = download.id, notification.id

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_download(service, work_id, provider, chapter):
        started.set()
        await release.wait()
        return "ready.cbz", DownloadVerification("ready.cbz", "digest", 1, 1, True)

    async def send_notifications(service):
        return {"delivered": 1}

    monkeypatch.setattr(worker_module.DownloadService, "download", slow_download)
    monkeypatch.setattr(worker_module.NotificationService, "deliver_pending", send_notifications)
    settings = Settings(poll_interval_seconds=0.1, max_job_attempts=1)
    providers = ProviderRegistry([])
    download_task = asyncio.create_task(DownloadWorker(settings, providers).run_once())
    await asyncio.wait_for(started.wait(), 2)
    assert await asyncio.wait_for(
        JobWorker(settings, providers, exclude_downloads=True).run_once(), 2
    )
    with factory() as session:
        assert session.get(Job, notification_id).status == JobStatus.SUCCEEDED
        assert session.get(Job, download_id).status == JobStatus.RUNNING

    # A SQLite writer may hold the lock while the download finishes. Recording
    # completion must retry without freezing the event loop or losing the result.
    with engine.connect() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        release.set()
        await asyncio.sleep(0.25)
        assert not download_task.done()
        connection.rollback()
    assert await asyncio.wait_for(download_task, 3)
    with factory() as session:
        finished = session.get(Job, download_id)
        assert finished.status == JobStatus.SUCCEEDED
        assert finished.payload["output_path"] == "ready.cbz"


def test_concurrent_claims_cannot_take_the_same_job(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from app.modules.jobs.repository import JobRepository

    engine = create_engine(
        f"sqlite:///{tmp_path / 'claim.db'}",
        connect_args={"check_same_thread": False, "timeout": 5},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        job = Job(kind=DOWNLOAD_CHAPTER, payload={})
        session.add(job)
        session.commit()
        expected_id = job.id
    barrier = Barrier(2)

    def claim():
        with factory() as session:
            barrier.wait(timeout=3)
            claimed = JobRepository(session).claim_next(kinds={DOWNLOAD_CHAPTER})
            return claimed.id if claimed else None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: claim(), range(2)))
    assert results.count(expected_id) == 1
    assert results.count(None) == 1
    with factory() as session:
        assert session.get(Job, expected_id).attempts == 1


@pytest.mark.asyncio
async def test_failed_download_keeps_retry_state(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'retry.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(worker_module, "SessionLocal", factory)
    with factory() as session:
        job = Job(kind=DOWNLOAD_CHAPTER, payload={"work_id": 1, "provider": "x", "chapter_id": "1"})
        session.add(job)
        session.commit()
        job_id = job.id

    async def failed_download(*args):
        raise RuntimeError("来源暂不可用")

    monkeypatch.setattr(worker_module.DownloadService, "download", failed_download)
    assert await DownloadWorker(Settings(max_job_attempts=2), ProviderRegistry([])).run_once()
    with factory() as session:
        job = session.get(Job, job_id)
        assert job.status == JobStatus.PENDING
        assert job.attempts == 1
        assert job.next_attempt_at is not None
        assert job.error == "来源暂不可用"


def test_general_queue_prioritizes_notifications_without_claiming_downloads():
    from app.modules.jobs.repository import DISCOVER_AUTHOR, JobRepository

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as session:
        session.add_all(
            [
                Job(kind=DOWNLOAD_CHAPTER, payload={}),
                Job(kind=DISCOVER_AUTHOR, payload={}),
                Job(kind=DELIVER_NOTIFICATIONS, payload={}),
            ]
        )
        session.commit()
        repository = JobRepository(session)
        first = repository.claim_next(
            exclude_kinds={DOWNLOAD_CHAPTER}, prioritize_notifications=True
        )
        second = repository.claim_next(
            exclude_kinds={DOWNLOAD_CHAPTER}, prioritize_notifications=True
        )
        assert first.kind == DELIVER_NOTIFICATIONS
        assert second.kind == DISCOVER_AUTHOR
