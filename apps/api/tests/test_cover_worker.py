import asyncio
import sqlite3
import threading

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.db.models import Job, JobStatus, Work, WorkFingerprint
from app.db.session import configure_sqlite_engine
from app.modules.catalog import cover_maintenance
from app.modules.catalog.cover_maintenance import CoverFingerprintRefreshService
from app.modules.jobs import worker as worker_module
from app.modules.jobs.repository import DELIVER_NOTIFICATIONS, REFRESH_COVER_FINGERPRINTS
from app.modules.jobs.worker import CoverWorkerThread, JobWorker
from app.providers.registry import ProviderRegistry


@pytest.mark.asyncio
async def test_changed_cover_does_not_receive_old_fingerprint(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'cover.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        work = Work(title="test", cover_url="https://old.example/cover.jpg")
        work.fingerprint = WorkFingerprint(normalized_title="test")
        session.add(work)
        session.commit()
        work_id = work.id

    async def change_cover(hasher, url, provider, external_id):
        assert url == "https://old.example/cover.jpg"
        with factory() as other:
            other.get(Work, work_id).cover_url = "https://new.example/cover.jpg"
            other.commit()
        return {"variants": [{"dhash": "oldhash"}]}

    monkeypatch.setattr(cover_maintenance.CoverHasher, "fingerprint_url", change_cover)
    with factory() as session:
        result = await CoverFingerprintRefreshService(session, ProviderRegistry([])).run()
    assert result["refreshed"] == 0
    assert result["remaining"] == 1
    with factory() as session:
        assert session.get(WorkFingerprint, work_id).cover_hash is None
        assert session.get(WorkFingerprint, work_id).cover_fingerprint is None


@pytest.mark.asyncio
async def test_slow_cover_index_does_not_block_general_queue(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'jobs.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(worker_module, "SessionLocal", factory)
    monkeypatch.setattr(worker_module, "build_registry", lambda settings: ProviderRegistry([]))
    with factory() as session:
        index = Job(kind=REFRESH_COVER_FINGERPRINTS, payload={})
        notification = Job(kind=DELIVER_NOTIFICATIONS, payload={})
        session.add_all([index, notification])
        session.commit()
        index_id, notification_id = index.id, notification.id

    started, release = threading.Event(), threading.Event()

    async def slow_index(service, force):
        started.set()
        await asyncio.to_thread(release.wait)
        return {"remaining": 0}

    async def deliver(service):
        return {"delivered": 1}

    monkeypatch.setattr(worker_module.CoverFingerprintRefreshService, "run", slow_index)
    monkeypatch.setattr(worker_module.NotificationService, "deliver_pending", deliver)
    settings = Settings(poll_interval_seconds=0.05)
    index_worker = CoverWorkerThread(settings)
    index_worker.start()
    try:
        assert await asyncio.to_thread(started.wait, 2)
        assert await asyncio.wait_for(
            JobWorker(settings, ProviderRegistry([]), exclude_downloads=True, exclude_covers=True)
            .run_once(), 2
        )
        with factory() as session:
            assert session.get(Job, index_id).status == JobStatus.RUNNING
            assert session.get(Job, notification_id).status == JobStatus.SUCCEEDED
    finally:
        release.set()
        await index_worker.stop()
    with factory() as session:
        assert session.get(Job, index_id).status == JobStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_cover_refresh_retries_sqlite_lock_without_redownloading(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'cover_retry.db'}", connect_args={"check_same_thread": False}
    )
    configure_sqlite_engine(engine)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        work = Work(title="cover-retry", cover_url="https://img.example/cover.jpg")
        work.fingerprint = WorkFingerprint(normalized_title="cover-retry")
        session.add(work)
        session.commit()
        work_id = work.id

    download_calls = 0

    async def fake_fingerprint(hasher, url, provider, external_id):
        nonlocal download_calls
        download_calls += 1
        return {"version": 2, "variants": [{"dhash": "abcd1234"}]}

    monkeypatch.setattr(cover_maintenance.CoverHasher, "fingerprint_url", fake_fingerprint)
    with factory() as session:
        original_commit = session.commit
        commit_attempts = 0

        def flaky_commit():
            nonlocal commit_attempts
            commit_attempts += 1
            if commit_attempts == 1:
                raise OperationalError(
                    "COMMIT", {}, sqlite3.OperationalError("database is locked")
                )
            return original_commit()

        monkeypatch.setattr(session, "commit", flaky_commit)
        result = await CoverFingerprintRefreshService(session, ProviderRegistry([])).run()

    assert download_calls == 1
    assert commit_attempts >= 2
    assert result["refreshed"] == 1
    with factory() as session:
        assert session.get(WorkFingerprint, work_id).cover_hash == "abcd1234"
