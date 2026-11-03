import asyncio
import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.db.base import Base
from app.db.models import DailyDigestDelivery, Job, JobStatus
from app.modules.jobs import worker as worker_module
from app.modules.jobs.repository import DELIVER_NOTIFICATIONS, DISCOVER_AUTHOR
from app.modules.jobs.worker import JobWorker, NotificationWorkerThread
from app.modules.social.notifications import NotificationService
from app.providers.registry import ProviderRegistry


@pytest.mark.asyncio
async def test_notification_runs_while_discovery_is_waiting(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'jobs.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(worker_module, "SessionLocal", factory)
    monkeypatch.setattr(worker_module, "build_registry", lambda settings: ProviderRegistry([]))
    with factory() as session:
        discovery = Job(kind=DISCOVER_AUTHOR, payload={"author_id": 1})
        notification = Job(kind=DELIVER_NOTIFICATIONS, payload={})
        session.add_all([discovery, notification])
        session.commit()
        discovery_id, notification_id = discovery.id, notification.id

    started, release, sent = asyncio.Event(), asyncio.Event(), threading.Event()

    async def slow_discovery(service, author_id):
        started.set()
        await release.wait()
        return {"found": 0}

    async def send(service):
        sent.set()
        return {"delivered": 1}

    monkeypatch.setattr(worker_module.DiscoveryService, "discover_author", slow_discovery)
    monkeypatch.setattr(worker_module.NotificationService, "deliver_pending", send)
    settings = Settings(poll_interval_seconds=0.05)
    general = JobWorker(
        settings, ProviderRegistry([]), exclude_downloads=True, exclude_covers=True,
        exclude_notifications=True,
    )
    discovery_task = asyncio.create_task(general.run_once())
    lane = NotificationWorkerThread(settings)
    try:
        await asyncio.wait_for(started.wait(), 2)
        lane.start()
        assert await asyncio.to_thread(sent.wait, 3)
        with factory() as session:
            assert session.get(Job, discovery_id).status == JobStatus.RUNNING
        release.set()
        await asyncio.wait_for(discovery_task, 3)
    finally:
        release.set()
        await lane.stop()
    with factory() as session:
        assert session.get(Job, notification_id).status == JobStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_database_lock_after_send_retries_save_without_resending(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'outbox.db'}",
        connect_args={"check_same_thread": False, "timeout": 0.05},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        delivery = DailyDigestDelivery(
            local_date="2026-11-03", timezone="UTC",
            period_start=datetime.now(UTC) - timedelta(days=1), period_end=datetime.now(UTC),
            author_ids=[], content_hash="hash", payload={"text": "hello"}, status="pending",
        )
        session.add(delivery)
        session.commit()
        delivery_id = delivery.id

    sent, proceed = threading.Event(), threading.Event()
    calls = []

    async def fake_send(service, channel, **kwargs):
        calls.append(channel)
        sent.set()
        await asyncio.to_thread(proceed.wait)

    monkeypatch.setattr(NotificationService, "_send", fake_send)
    settings = Settings(qq_bot_enabled=True, poll_interval_seconds=0.1)

    def deliver():
        with factory() as session:
            return asyncio.run(NotificationService(session, settings).deliver_pending())

    task = asyncio.create_task(asyncio.to_thread(deliver))
    assert await asyncio.to_thread(sent.wait, 2)
    with engine.connect() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        proceed.set()
        await asyncio.sleep(0.4)
        assert not task.done()
        connection.rollback()
    result = await asyncio.wait_for(task, 3)
    assert result["delivered"] == 1
    assert calls == ["qq"]
    with factory() as session:
        saved = session.get(DailyDigestDelivery, delivery_id)
        assert saved.status == "delivered"
        assert saved.attempts == 1
        assert saved.payload["_delivered_channels"] == ["qq"]
