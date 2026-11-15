import asyncio
import sqlite3
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.db.models import (
    Author,
    AuthorDigest,
    Job,
    JobStatus,
    NotificationOutbox,
    SocialAccount,
    SocialPost,
    SocialPostProcessing,
)
from app.db.session import configure_sqlite_engine
from app.modules.jobs import worker as worker_module
from app.modules.jobs.repository import (
    DISCOVER_AUTHOR,
    REFRESH_COVER_FINGERPRINTS,
    JobRepository,
)
from app.modules.jobs.worker import CoverWorkerThread, JobWorker, SocialProcessingWorkerThread
from app.providers.registry import ProviderRegistry


def test_sqlite_engine_enables_wal_busy_timeout_and_foreign_keys(tmp_path) -> None:
    db_path = tmp_path / "wal.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 30.0},
    )
    configure_sqlite_engine(engine)
    with engine.connect() as connection:
        journal_mode = connection.exec_driver_sql("PRAGMA journal_mode").scalar_one()
        busy_timeout = connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one()
        foreign_keys = connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one()
        synchronous = connection.exec_driver_sql("PRAGMA synchronous").scalar_one()

    assert str(journal_mode).lower() == "wal"
    assert int(busy_timeout) == 30000
    assert int(foreign_keys) == 1
    assert int(synchronous) == 1


@pytest.mark.asyncio
async def test_succeed_lock_retry_does_not_rerun_or_fail_job(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "succeed_retry.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    configure_sqlite_engine(engine)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    with factory() as session:
        author = Author(name="作者")
        session.add(author)
        session.flush()
        account = SocialAccount(author_id=author.id, handle="creator", status="confirmed")
        session.add(account)
        session.flush()
        post = SocialPost(
            account_id=account.id,
            platform_post_id="100",
            text="新刊",
            url="https://x.com/creator/status/100",
            media=[],
            links=[],
            raw_metadata={},
            content_hash="h100",
            posted_at=datetime.now(UTC),
        )
        session.add(post)
        session.flush()
        session.add(SocialPostProcessing(post_id=post.id, content_hash="h100"))
        job = JobRepository(session).enqueue_social_post(post.id)
        session.commit()
        post_id, job_id = post.id, job.id

    process_calls = 0

    async def fake_process_post(service, incoming_post_id: int):
        nonlocal process_calls
        process_calls += 1
        assert incoming_post_id == post_id
        state = service.session.get(SocialPostProcessing, incoming_post_id)
        assert state is not None
        state.status = "done"
        service.session.commit()
        return {"status": "done", "activity": True, "signal": False}

    succeed_attempts = 0
    original_succeed = JobRepository.succeed

    def flaky_succeed(self, job: Job) -> None:
        nonlocal succeed_attempts
        succeed_attempts += 1
        if succeed_attempts == 1:
            raise OperationalError(
                "COMMIT", {}, sqlite3.OperationalError("database is locked")
            )
        return original_succeed(self, job)

    monkeypatch.setattr(worker_module, "SessionLocal", factory)
    monkeypatch.setattr(worker_module.SocialSyncService, "process_post", fake_process_post)
    monkeypatch.setattr(JobRepository, "succeed", flaky_succeed)

    worker = JobWorker(
        Settings(poll_interval_seconds=0.05, max_job_attempts=3),
        ProviderRegistry([]),
        social_processing_only=True,
    )
    assert await worker.run_once() is True
    assert process_calls == 1
    assert succeed_attempts == 2

    with factory() as session:
        finished = session.get(Job, job_id)
        assert finished is not None
        assert finished.status == JobStatus.SUCCEEDED
        assert finished.payload["status"] == "done"


@pytest.mark.asyncio
async def test_main_worker_claim_and_fail_retry_on_sqlite_lock(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "fail_retry.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    configure_sqlite_engine(engine)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    with factory() as session:
        job = Job(kind=DISCOVER_AUTHOR, payload={"author_id": 1})
        session.add(job)
        session.commit()
        job_id = job.id

    claim_attempts = 0
    original_claim = JobRepository.claim_next

    def flaky_claim(self, **kwargs):
        nonlocal claim_attempts
        claim_attempts += 1
        if claim_attempts == 1:
            raise OperationalError(
                "UPDATE", {}, sqlite3.OperationalError("database is locked")
            )
        return original_claim(self, **kwargs)

    async def failing_discover(service, _author_id: int):
        raise RuntimeError("boom")

    fail_attempts = 0
    original_fail = JobRepository.fail

    def flaky_fail(self, job: Job, error: str, max_attempts: int) -> None:
        nonlocal fail_attempts
        fail_attempts += 1
        if fail_attempts == 1:
            raise OperationalError(
                "COMMIT", {}, sqlite3.OperationalError("database is locked")
            )
        return original_fail(self, job, error, max_attempts)

    monkeypatch.setattr(worker_module, "SessionLocal", factory)
    monkeypatch.setattr(JobRepository, "claim_next", flaky_claim)
    monkeypatch.setattr(JobRepository, "fail", flaky_fail)
    monkeypatch.setattr(worker_module.DiscoveryService, "discover_author", failing_discover)

    # Main lane (exclude_downloads=True, no *_only flags) must also survive lock errors.
    worker = JobWorker(
        Settings(poll_interval_seconds=0.05, max_job_attempts=1),
        ProviderRegistry([]),
        exclude_downloads=True,
        exclude_covers=True,
        exclude_notifications=True,
        exclude_social_processing=True,
    )
    assert await worker.run_once() is True
    assert claim_attempts == 2
    assert fail_attempts == 2

    with factory() as session:
        failed = session.get(Job, job_id)
        assert failed is not None
        assert failed.status == JobStatus.FAILED
        assert failed.error == "boom"


@pytest.mark.asyncio
async def test_refresh_digest_commits_before_succeed_lock_retry(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "digest_retry.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    configure_sqlite_engine(engine)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    with factory() as session:
        author = Author(name="作者")
        session.add(author)
        session.flush()
        job = JobRepository(session).enqueue_social_digest(author.id)
        session.commit()
        author_id, job_id = author.id, job.id

    async def fake_refresh(service, incoming_author_id: int):
        digest = AuthorDigest(
            author_id=incoming_author_id,
            period_type="rolling_7d",
            period_start=datetime.now(UTC),
            period_end=datetime.now(UTC),
            summary="近期有新动态",
            highlights=[],
            uncertainties=[],
            evidence_post_ids=[],
            content_hash="digest-hash",
            generated_by="rules",
            prompt_version="v1",
        )
        service.session.add(digest)
        service.session.flush()
        return digest

    succeed_attempts = 0
    original_succeed = JobRepository.succeed

    def flaky_succeed(self, job: Job) -> None:
        nonlocal succeed_attempts
        succeed_attempts += 1
        if succeed_attempts == 1:
            raise OperationalError(
                "COMMIT", {}, sqlite3.OperationalError("database is locked")
            )
        return original_succeed(self, job)

    monkeypatch.setattr(worker_module, "SessionLocal", factory)
    monkeypatch.setattr(worker_module.DigestService, "refresh", fake_refresh)
    monkeypatch.setattr(JobRepository, "succeed", flaky_succeed)

    worker = JobWorker(
        Settings(poll_interval_seconds=0.05),
        ProviderRegistry([]),
        social_processing_only=True,
    )
    assert await worker.run_once() is True
    assert succeed_attempts == 2

    with factory() as session:
        assert session.get(Job, job_id).status == JobStatus.SUCCEEDED
        saved_digest = session.scalar(
            select(AuthorDigest).where(AuthorDigest.author_id == author_id)
        )
        assert saved_digest is not None


@pytest.mark.asyncio
async def test_concurrent_cover_and_social_workers_complete_without_lock_failures(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "concurrent.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 30.0},
    )
    configure_sqlite_engine(engine)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    with factory() as session:
        author = Author(name="并发作者")
        session.add(author)
        session.flush()
        account = SocialAccount(author_id=author.id, handle="creator", status="confirmed")
        session.add(account)
        session.flush()
        for idx in range(5):
            post = SocialPost(
                account_id=account.id,
                platform_post_id=f"p{idx}",
                text=f"C108 新刊 {idx}",
                url=f"https://x.com/creator/status/{idx}",
                media=[],
                links=[],
                raw_metadata={},
                content_hash=f"h{idx}",
                posted_at=datetime.now(UTC),
            )
            session.add(post)
            session.flush()
            session.add(SocialPostProcessing(post_id=post.id, content_hash=f"h{idx}"))
            JobRepository(session).enqueue_social_post(post.id)
        for idx in range(3):
            session.add(Job(kind=REFRESH_COVER_FINGERPRINTS, payload={"batch": idx}))
        session.commit()

    async def fast_cover(service, force: bool):
        with factory() as other:
            assert other.scalar(select(Author.name)) == "并发作者"
        await asyncio.sleep(0.01)
        return {"remaining": 0, "refreshed": 1}

    async def fast_post(service, post_id: int):
        state = service.session.get(SocialPostProcessing, post_id)
        assert state is not None
        state.status = "done"
        service.session.commit()
        return {"status": "done"}

    monkeypatch.setattr(worker_module, "SessionLocal", factory)
    monkeypatch.setattr(worker_module, "build_registry", lambda settings: ProviderRegistry([]))
    monkeypatch.setattr(worker_module.CoverFingerprintRefreshService, "run", fast_cover)
    monkeypatch.setattr(worker_module.SocialSyncService, "process_post", fast_post)

    settings = Settings(poll_interval_seconds=0.02)
    cover_worker = CoverWorkerThread(settings)
    social_worker = SocialProcessingWorkerThread(settings)
    cover_worker.start()
    social_worker.start()
    try:
        for _ in range(100):
            with factory() as session:
                pending = session.scalar(
                    select(Job.id).where(Job.status.in_([JobStatus.PENDING, JobStatus.RUNNING]))
                )
                if pending is None:
                    break
            await asyncio.sleep(0.05)
    finally:
        await cover_worker.stop()
        await social_worker.stop()

    with factory() as session:
        jobs = list(session.scalars(select(Job)))
        assert len(jobs) == 8
        assert all(job.status == JobStatus.SUCCEEDED for job in jobs)
        assert session.scalar(select(NotificationOutbox)) is None
