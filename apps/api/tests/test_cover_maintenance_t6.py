from datetime import UTC, datetime, timedelta

import pytest
from app.core.config import Settings
from app.core.observability import capture_metrics
from app.db import session as db_session
from app.db.base import Base
from app.db.models import (
    Author,
    AuthorAggregationState,
    AuthorWork,
    CoverMaintenanceState,
    Job,
    JobStatus,
    Work,
    WorkFingerprint,
    WorkSource,
)
from app.db.session import configure_sqlite_engine
from app.modules.catalog import cover_maintenance
from app.modules.catalog.cover_maintenance import CoverFingerprintRefreshService
from app.modules.jobs import worker as worker_module
from app.modules.jobs.repository import JobRepository
from app.modules.jobs.worker import JobWorker
from app.providers.registry import ProviderRegistry
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.orm import sessionmaker


def _seed_works(
    factory: sessionmaker,
    count: int,
    *,
    already_valid: int = 0,
    with_author: bool = False,
) -> list[int]:
    work_ids: list[int] = []
    with factory() as session:
        author = None
        if with_author:
            author = Author(name="Cover Artist")
            session.add(author)
            session.flush()
        for idx in range(count):
            url = f"https://img.example/{idx}.jpg"
            fp_payload = (
                {
                    "version": 2,
                    "source_url": url,
                    "variants": [{"kind": "full", "ratio": 1.0, "dhash": "abcd1234"}],
                }
                if idx < already_valid
                else None
            )
            work = Work(title=f"Work {idx}", cover_url=url)
            work.fingerprint = WorkFingerprint(
                normalized_title=f"work {idx}",
                cover_fingerprint=fp_payload,
                cover_hash="abcd1234" if fp_payload else None,
            )
            work.sources.append(
                WorkSource(
                    provider="stub",
                    external_id=str(idx),
                    source_url=f"https://src.example/{idx}",
                )
            )
            session.add(work)
            session.flush()
            if author is not None:
                session.add(AuthorWork(author_id=author.id, work_id=work.id))
            work_ids.append(work.id)
        session.commit()
    return work_ids


@pytest.mark.asyncio
async def test_keyset_pagination_bounds_batch_and_reaches_tail_past_valid_rows(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = create_engine(
        f"sqlite:///{tmp_path / 't6-tail.db'}", connect_args={"check_same_thread": False}
    )
    configure_sqlite_engine(engine)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    # First 80 rows already valid, last 15 rows need refresh (batch_size=20).
    work_ids = _seed_works(factory, 95, already_valid=80)
    fetched: list[str] = []

    async def fake_fingerprint(hasher, url, provider, external_id):
        fetched.append(url)
        return {"version": 2, "variants": [{"kind": "full", "ratio": 1.0, "dhash": "11112222"}]}

    monkeypatch.setattr(cover_maintenance.CoverHasher, "fingerprint_url", fake_fingerprint)

    with factory() as session:
        with capture_metrics("t6.tail") as metrics:
            res = await CoverFingerprintRefreshService(session, ProviderRegistry([])).run(
                force=False, batch_size=20
            )
        snap = metrics.snapshot()

    assert res["refreshed"] == 15
    assert res["has_more"] is False
    assert res["next_cursor"] is None
    assert res["round_status"] == "completed"
    assert res["remaining"] == 0
    # Must never misreport page counts as full-table totals when scanning, or on completion:
    assert res["processed"] == 15
    assert snap["counters"]["cover_works_refreshed"] == 15
    assert len(fetched) == 15

    # Tail works 80..94 are now fingerprinted.
    with factory() as session:
        for wid in work_ids[80:]:
            assert session.get(WorkFingerprint, wid).cover_hash == "11112222"
        # Next run when everything is valid walks to the end with 0 downloads.
        res_noop = await CoverFingerprintRefreshService(session, ProviderRegistry([])).run(
            force=False, batch_size=20
        )
    assert res_noop["processed"] == 0
    assert res_noop["refreshed"] == 0
    assert res_noop["has_more"] is False
    assert res_noop["round_status"] == "completed"
    assert len(fetched) == 15
    engine.dispose()


@pytest.mark.asyncio
async def test_force_round_batches_resumes_after_restart_and_respects_max_work_id(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "t6-force.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    configure_sqlite_engine(engine)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    work_ids = _seed_works(factory, 55, already_valid=55, with_author=True)
    processed_urls: list[str] = []

    async def fake_fingerprint(hasher, url, provider, external_id):
        processed_urls.append(url)
        return {"version": 2, "variants": [{"kind": "full", "ratio": 1.0, "dhash": "9999aaaa"}]}

    monkeypatch.setattr(cover_maintenance.CoverHasher, "fingerprint_url", fake_fingerprint)
    monkeypatch.setattr(worker_module, "SessionLocal", factory)
    monkeypatch.setattr(
        cover_maintenance.CoverFingerprintRefreshService,
        "run",
        lambda self, force=False, batch_size=20: CoverFingerprintRefreshService._run_impl(
            self, force=force, batch_size=20
        ),
    )

    settings = Settings(poll_interval_seconds=0.05, agent_enabled=False, _env_file=None)
    with factory() as session:
        JobRepository(session).enqueue_cover_fingerprint_refresh(force=True)
        session.commit()

    # Run batch 1 via JobWorker: should process only 20 works and enqueue continuation job.
    worker1 = JobWorker(settings, ProviderRegistry([]), covers_only=True)
    assert await worker1.run_once() is True

    with factory() as session:
        state = session.get(CoverMaintenanceState, "force")
        assert state is not None
        assert state.cursor_work_id == work_ids[19]
        assert state.max_work_id == work_ids[-1]
        assert len(processed_urls) == 20
        jobs = list(session.scalars(select(Job).order_by(Job.id)))
        assert len(jobs) == 2
        assert jobs[0].status == JobStatus.SUCCEEDED
        assert jobs[0].payload["has_more"] is True
        assert jobs[0].payload["next_cursor"] == work_ids[19]
        assert jobs[0].payload["eligible"] is None
        assert jobs[0].payload["pending"] is None
        assert jobs[1].status == JobStatus.PENDING
        assert jobs[1].payload["force"] is True

        # Insert 10 brand-new works mid-round; max_work_id bounds the current force round.
        for extra in range(10):
            w = Work(title=f"Extra {extra}", cover_url=f"https://img.example/extra-{extra}.jpg")
            w.fingerprint = WorkFingerprint(normalized_title=f"extra {extra}")
            session.add(w)
        session.commit()

    # Simulate worker restart with a new JobWorker instance; finish remaining batches (20 + 15).
    worker2 = JobWorker(settings, ProviderRegistry([]), covers_only=True)
    assert await worker2.run_once() is True  # works 20..39
    assert await worker2.run_once() is True  # works 40..54 -> finishes round at max_work_id!
    assert await worker2.run_once() is False  # no infinite loop from extra works

    assert len(processed_urls) == 55
    with factory() as session:
        state = session.get(CoverMaintenanceState, "force")
        assert state is not None
        assert state.max_work_id is None
        assert state.cursor_work_id == 0
        last_job = list(session.scalars(select(Job).order_by(Job.id)))[-1]
        assert last_job.payload["round_status"] == "completed"
        assert last_job.payload["has_more"] is False
        # T4 author aggregation invalidation bumped when fingerprints updated.
        author = session.scalar(select(Author))
        agg_state = session.get(AuthorAggregationState, author.id)
        assert agg_state is not None
        assert agg_state.input_revision > agg_state.completed_revision
    engine.dispose()


@pytest.mark.asyncio
async def test_failed_covers_do_not_starve_subsequent_works_and_have_bounded_retries(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = create_engine(
        f"sqlite:///{tmp_path / 't6-fail.db'}", connect_args={"check_same_thread": False}
    )
    configure_sqlite_engine(engine)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    # Work 0: permanent failure; Work 1: transient failure (succeeds on retry); Works 2..24: ok.
    work_ids = _seed_works(factory, 25, already_valid=0)
    attempts_by_url: dict[str, int] = {}

    async def flaky_fingerprint(hasher, url, provider, external_id):
        attempts_by_url[url] = attempts_by_url.get(url, 0) + 1
        if url.endswith("/0.jpg"):
            return None  # Permanent failure
        if url.endswith("/1.jpg") and attempts_by_url[url] == 1:
            raise RuntimeError("transient 502")
        return {"version": 2, "variants": [{"kind": "full", "ratio": 1.0, "dhash": "feedface"}]}

    monkeypatch.setattr(cover_maintenance.CoverHasher, "fingerprint_url", flaky_fingerprint)

    with factory() as session:
        svc = CoverFingerprintRefreshService(session, ProviderRegistry([]))
        # Batch 1 (0..9): 0 and 1 fail, 2..9 succeed -> cursor advances to work_ids[9].
        r1 = await svc.run(force=False, batch_size=10)
        assert r1["refreshed"] == 8
        assert r1["failed"] == 2
        assert r1["has_more"] is True
        assert r1["phase"] == "scan"
        assert r1["round_status"] == "in_progress"
        assert r1["retry_pending"] == 2

        # Batch 2 (10..19) & Batch 3 (20..24): good works must NOT be starved by 0 and 1.
        r2 = await svc.run(force=False, batch_size=10)
        assert r2["refreshed"] == 10
        assert r2["has_more"] is True
        r3 = await svc.run(force=False, batch_size=10)
        assert r3["refreshed"] == 5
        assert r3["has_more"] is True
        assert r3["phase"] == "retry"
        assert r3["retry_pending"] == 2

        # Retry batch 1: work 0 fails (attempt 2), work 1 succeeds!
        r4 = await svc.run(force=False, batch_size=10)
        assert r4["refreshed"] == 1
        assert r4["failed"] == 1
        assert r4["has_more"] is True
        assert r4["phase"] == "retry"
        assert r4["retry_pending"] == 1

        # Retry batch 2: work 0 fails (attempt 3 == max), enters cooldown and round completes!
        r5 = await svc.run(force=False, batch_size=10)
        assert r5["refreshed"] == 0
        assert r5["failed"] == 1
        assert r5["has_more"] is False
        assert r5["round_status"] == "completed_with_failures"
        assert r5["deferred_failures"] == 1
        assert r5["remaining"] == 0

        # Subsequent incremental run does not spin on cooled-down work 0.
        r6 = await svc.run(force=False, batch_size=10)
        assert r6["processed"] == 0
        assert r6["has_more"] is False
        assert r6["round_status"] == "completed_with_failures"
        assert r6["deferred_failures"] == 1
        assert attempts_by_url["https://img.example/0.jpg"] == 3

        # If work 0 gets a new cover_url (or cooldown expires), it becomes eligible immediately.
        w0 = session.get(Work, work_ids[0])
        w0.cover_url = "https://img.example/0-fixed.jpg"
        session.commit()
        r7 = await svc.run(force=False, batch_size=10)
        assert r7["refreshed"] == 1
        assert r7["has_more"] is False
        assert r7["round_status"] == "completed"

        # If cooldown expires on a previously failed work with same URL, it retries again.
        fp1 = session.get(WorkFingerprint, work_ids[1])
        fp1.cover_fingerprint = None
        fp1.cover_failed_url = "https://img.example/1.jpg"
        fp1.cover_failure_count = 3
        fp1.cover_next_retry_at = datetime.now(UTC) - timedelta(minutes=1)
        session.commit()
        r8 = await svc.run(force=False, batch_size=10)
        assert r8["refreshed"] == 1
        assert r8["round_status"] == "completed"
    engine.dispose()


def test_additive_migration_adds_t6_columns_and_state_table(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = create_engine(
        f"sqlite:///{tmp_path / 't6-migrate.db'}", connect_args={"check_same_thread": False}
    )
    configure_sqlite_engine(engine)
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for col in ("cover_failure_count", "cover_next_retry_at", "cover_failed_url"):
            conn.exec_driver_sql(f"ALTER TABLE work_fingerprints DROP COLUMN {col}")
        conn.exec_driver_sql("DROP TABLE cover_maintenance_state")

    monkeypatch.setattr(db_session, "engine", engine)
    db_session.init_db()

    inspector = inspect(engine)
    fp_cols = {col["name"] for col in inspector.get_columns("work_fingerprints")}
    assert {"cover_failure_count", "cover_next_retry_at", "cover_failed_url"} <= fp_cols
    assert "cover_maintenance_state" in set(inspector.get_table_names())
    engine.dispose()
