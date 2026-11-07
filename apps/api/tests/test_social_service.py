from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.base import Base
from app.db.models import Author, ReleaseSignal, SocialAccount, SocialPost
from app.modules.social.collector import CollectorBatch, CollectorPost
from app.modules.social.repository import SocialRepository
from app.modules.social.router import _account_read
from app.modules.social.schemas import SocialAccountCreate
from app.modules.social.service import SocialSyncService


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as value:
        yield value


def test_social_account_api_exposes_only_same_origin_avatar_url(session: Session) -> None:
    author = Author(name="头像作者")
    session.add(author)
    session.flush()
    account = SocialAccount(
        author_id=author.id,
        platform="x",
        handle="avatar_creator",
        status="confirmed",
        avatar_url="https://pbs.twimg.com/profile_images/2/avatar.jpg",
    )
    session.add(account)
    session.flush()

    assert _account_read(account).avatar_url == f"/api/social/accounts/{account.id}/avatar"


@pytest.mark.asyncio
async def test_ingest_does_not_auto_confirm_event_only(session: Session, tmp_path) -> None:
    author = Author(name="テスト作家")
    session.add(author)
    session.flush()
    account = SocialAccount(author_id=author.id, platform="x", handle="creator", status="confirmed")
    session.add(account)
    session.flush()
    settings = Settings(
        social_agent_enabled=False,
        social_ocr_enabled=False,
        social_media_dir=tmp_path,
    )
    incoming = [
        CollectorPost(
            id="10",
            text="C108に参加します。東A-12aです",
            url="https://x.com/creator/status/10",
            posted_at=datetime.now(UTC),
        )
    ]
    service = SocialSyncService(session, settings)
    service.queue_posts(account, incoming)
    session.commit()
    post = session.scalar(select(SocialPost))
    result = await service.process_post(post.id)
    signal = session.scalar(select(ReleaseSignal))
    assert result["signal"] is True
    assert signal is not None
    assert signal.kind == "event_participation"
    assert signal.status == "archived"


@pytest.mark.asyncio
async def test_ingest_auto_confirms_strong_new_release_without_catalog_write(
    session: Session, tmp_path
) -> None:
    author = Author(name="テスト作家")
    session.add(author)
    session.flush()
    account = SocialAccount(author_id=author.id, platform="x", handle="creator", status="confirmed")
    session.add(account)
    session.flush()
    settings = Settings(
        social_agent_enabled=False,
        social_ocr_enabled=False,
        social_media_dir=tmp_path,
        social_media_cache_max_bytes=0,
        social_auto_confirm_threshold=0.92,
    )
    incoming = [
        CollectorPost(
            id="11",
            text="C108新刊『星の本』の表紙です。BOOTHで予約開始",
            url="https://x.com/creator/status/11",
            posted_at=datetime.now(UTC),
            media=[{"type": "image", "url": "https://pbs.twimg.com/media/a.jpg"}],
            links=["https://creator.booth.pm/items/123"],
        )
    ]
    service = SocialSyncService(session, settings)
    service.queue_posts(account, incoming)
    session.commit()
    post = session.scalar(select(SocialPost))
    await service.process_post(post.id)
    signal = session.scalar(select(ReleaseSignal))
    assert signal is not None
    assert signal.status == "confirmed"
    assert signal.linked_group_id is None


@pytest.mark.asyncio
async def test_sync_failure_rolls_back_partial_writes_and_keeps_error_status(
    session: Session, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    author = Author(name="テスト作家")
    session.add(author)
    session.flush()
    account = SocialAccount(author_id=author.id, platform="x", handle="creator", status="confirmed")
    session.add(account)
    session.commit()
    account_id = account.id

    async def fake_posts(*args, **kwargs):
        return CollectorBatch(posts=[], complete=True)

    async def fake_profile(*args, **kwargs):
        return None

    def failing_ingest(service, current_account, incoming, **kwargs):
        service.session.add(
            SocialPost(
                account_id=current_account.id,
                platform_post_id="partial",
                post_type="original",
                text="不应提交",
                url="https://x.com/creator/status/partial",
                media=[],
                links=[],
                raw_metadata={},
                content_hash="partial",
                posted_at=datetime.now(UTC),
            )
        )
        service.session.flush()
        raise RuntimeError("摘要生成失败")

    monkeypatch.setattr("app.modules.social.service.XBrowserCollector.posts_batch", fake_posts)
    monkeypatch.setattr("app.modules.social.service.XBrowserCollector.profile", fake_profile)
    monkeypatch.setattr(SocialSyncService, "queue_posts", failing_ingest)
    settings = Settings(
        social_agent_enabled=False,
        social_ocr_enabled=False,
        social_media_dir=tmp_path,
    )

    with pytest.raises(RuntimeError, match="摘要生成失败"):
        await SocialSyncService(session, settings).sync_account(account_id)

    partial = session.scalar(select(SocialPost).where(SocialPost.platform_post_id == "partial"))
    assert partial is None
    saved = session.get(SocialAccount, account_id)
    assert saved is not None
    assert saved.sync_error == "摘要生成失败"
    assert saved.next_sync_at is not None


def test_adding_an_existing_suggestion_can_confirm_it(session: Session) -> None:
    author = Author(name="テスト作家")
    session.add(author)
    session.flush()
    repository = SocialRepository(session)
    suggested = repository.add_account(
        author.id, "Creator", "personal", False, display_name="旧名称"
    )

    confirmed = repository.add_account(author.id, "creator", "circle", True, display_name="新名称")

    assert confirmed.id == suggested.id
    assert confirmed.status == "confirmed"
    assert confirmed.account_type == "circle"
    assert confirmed.display_name == "新名称"
    assert confirmed.next_sync_at is not None


@pytest.mark.parametrize("handle", ["creator/name", "creator name", "x" * 16, "💥"])
def test_social_account_rejects_invalid_x_handles(handle: str) -> None:
    with pytest.raises(ValueError, match="X 账号格式不正确"):
        SocialAccountCreate(handle=handle)


@pytest.mark.asyncio
async def test_batched_sync_keeps_watermark_until_catchup_and_survives_failure(
    session: Session, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.db.models import SocialSyncProgress

    author = Author(name="artist")
    session.add(author)
    session.flush()
    account = SocialAccount(
        author_id=author.id, handle="creator", status="confirmed", last_post_id="1"
    )
    session.add(account)
    session.commit()
    account_id = account.id
    seen = []
    now = datetime.now(UTC)

    async def fake_batch(self, handle, since_id, limit, cursor=None, stop_before=None):
        seen.append((since_id, cursor))
        if len(seen) == 2:
            raise RuntimeError("temporary outage")
        if cursor is None:
            return CollectorBatch(
                posts=[CollectorPost(id="3", text="a", url="https://x.test/3", posted_at=now)],
                complete=False,
                next_cursor="graphql:next",
            )
        return CollectorBatch(
            posts=[CollectorPost(id="2", text="b", url="https://x.test/2", posted_at=now)],
            complete=True,
        )

    async def fake_profile(*args, **kwargs):
        return None

    monkeypatch.setattr("app.modules.social.service.XBrowserCollector.posts_batch", fake_batch)
    monkeypatch.setattr("app.modules.social.service.XBrowserCollector.profile", fake_profile)
    settings = Settings(
        social_agent_enabled=False, social_ocr_enabled=False, social_media_dir=tmp_path
    )
    service = SocialSyncService(session, settings)
    first = await service.sync_account(account_id)
    assert first["complete"] is False
    assert session.get(SocialAccount, account_id).last_post_id == "1"
    assert session.get(SocialSyncProgress, account_id).cursor == "graphql:next"
    with pytest.raises(RuntimeError, match="temporary outage"):
        await service.sync_account(account_id)
    assert session.get(SocialAccount, account_id).last_post_id == "1"
    assert session.get(SocialSyncProgress, account_id).cursor == "graphql:next"
    last = await service.sync_account(account_id)
    assert last["complete"] is True
    assert session.get(SocialAccount, account_id).last_post_id == "3"
    assert session.get(SocialSyncProgress, account_id) is None
    assert seen == [("1", None), ("1", "graphql:next"), ("1", "graphql:next")]
    assert {p.platform_post_id for p in session.scalars(select(SocialPost))} == {"2", "3"}


@pytest.mark.asyncio
async def test_social_processing_releases_write_lock_during_ocr_and_model(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3
    from types import SimpleNamespace

    from app.db.models import ActivityItem, Job, SocialPostProcessing
    from app.modules.social.ocr import OcrResult
    from app.modules.social.schemas import SocialAgentVerdict

    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    settings = Settings(
        social_agent_enabled=True,
        agent_enabled=True,
        agent_base_url="https://example.test",
        agent_model="model",
        agent_api_key="test",
        social_ocr_enabled=True,
        social_media_dir=tmp_path,
        social_media_cache_max_bytes=0,
    )
    checkpoints = []

    def prove_other_writer_can_acquire_lock(label):
        with sqlite3.connect(db_path, timeout=0.1) as other:
            other.execute("BEGIN IMMEDIATE")
            other.rollback()
        checkpoints.append(label)

    async def fake_ocr(self, media, local_paths=None):
        prove_other_writer_can_acquire_lock("ocr")
        return OcrResult("text", "C108 新刊 サンプル")

    async def fake_review(self, evidence):
        prove_other_writer_can_acquire_lock("model")
        return SimpleNamespace(
            verdict=SocialAgentVerdict(
                kind="new_release",
                is_authors_work=True,
                has_new_work=True,
                confidence=0.8,
                rationale="test",
            )
        )

    monkeypatch.setattr("app.modules.social.ocr.LocalMediaOcr.extract_result", fake_ocr)
    monkeypatch.setattr("app.modules.social.agent.SocialReleaseReviewer.review", fake_review)
    with Session(engine, expire_on_commit=False) as session:
        author = Author(name="artist")
        session.add(author)
        session.flush()
        account = SocialAccount(
            author_id=author.id, handle="creator", status="confirmed", last_post_id="1"
        )
        session.add(account)
        session.commit()
        service = SocialSyncService(session, settings)
        item = CollectorPost(
            id="2",
            text="新刊予約開始",
            url="https://x.test/2",
            posted_at=datetime.now(UTC),
            media=[{"type": "image", "url": "https://pbs.twimg.com/media/x.jpg"}],
        )
        service.queue_posts(account, [item])
        session.commit()
        post = session.scalar(select(SocialPost))
        assert post is not None
        result = await service.process_post(post.id)
        assert result["status"] == "done" and checkpoints == ["ocr", "model"]
        assert session.get(SocialPostProcessing, post.id).ocr_status == "text"
        assert session.scalar(select(ActivityItem)) is not None
        assert session.scalar(select(Job).where(Job.kind == "refresh_social_digest")) is not None
        await service.process_post(post.id)
        assert checkpoints == ["ocr", "model"]


@pytest.mark.asyncio
async def test_historical_ocr_backfill_does_not_send_old_notifications(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import timedelta

    from sqlalchemy.orm import sessionmaker

    from app.db.models import ActivityItem, Job, NotificationOutbox, SocialPostProcessing
    from app.modules.social.ocr import OcrResult
    from app.modules.social.scheduler import SocialScheduler

    engine = create_engine(f"sqlite:///{tmp_path / 'backfill.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    settings = Settings(
        social_agent_enabled=False,
        social_ocr_enabled=True,
        social_media_dir=tmp_path,
        social_media_cache_max_bytes=0,
        social_daily_digest_enabled=False,
    )
    with factory() as session:
        author = Author(name="artist")
        session.add(author)
        session.flush()
        account = SocialAccount(
            author_id=author.id,
            handle="creator",
            status="confirmed",
            next_sync_at=datetime.now(UTC) + timedelta(days=1),
        )
        session.add(account)
        session.flush()
        post = SocialPost(
            account_id=account.id,
            platform_post_id="old",
            post_type="original",
            text="",
            url="https://x.test/old",
            posted_at=datetime.now(UTC) - timedelta(days=20),
            content_hash="old",
            links=[],
            raw_metadata={},
            media=[{"type": "image", "url": "https://pbs.twimg.com/media/old.jpg"}],
        )
        session.add(post)
        session.commit()
        post_id = post.id

    monkeypatch.setattr("app.modules.social.scheduler.SessionLocal", factory)

    async def fake_ocr(self, media, local_paths=None):
        return OcrResult("text", "C108 新刊 サンプル 予約開始")

    monkeypatch.setattr("app.modules.social.ocr.LocalMediaOcr.extract_result", fake_ocr)
    SocialScheduler(settings).enqueue_due()
    with factory() as session:
        state = session.get(SocialPostProcessing, post_id)
        assert state and state.backfill_only and state.status == "pending"
        assert session.scalar(select(Job).where(Job.kind == "social_process_post")) is not None
        await SocialSyncService(session, settings).process_post(post_id)
        assert session.get(SocialPostProcessing, post_id).ocr_status == "text"
        assert session.scalar(select(NotificationOutbox)) is None
        assert session.scalar(select(ActivityItem)).is_read is True
        signal = session.scalar(select(ReleaseSignal))
        assert signal is None or signal.status != "confirmed"


@pytest.mark.asyncio
async def test_ocr_result_distinguishes_empty_from_retry(tmp_path, monkeypatch):
    from app.modules.social.ocr import LocalMediaOcr

    settings = Settings(social_ocr_enabled=True, social_media_dir=tmp_path)
    ocr = LocalMediaOcr(settings)
    monkeypatch.setattr("app.modules.social.ocr.shutil.which", lambda _: "/usr/bin/tesseract")
    media = [{"type": "image", "url": "https://pbs.twimg.com/media/x.jpg"}]

    async def empty(path):
        return b""

    monkeypatch.setattr(ocr, "_recognize", empty)
    assert (await ocr.extract_result(media, {0: tmp_path / "image.jpg"})).status == "empty"

    async def failure(path):
        return None

    monkeypatch.setattr(ocr, "_recognize", failure)
    assert (await ocr.extract_result(media, {0: tmp_path / "image.jpg"})).status == "retry"


@pytest.mark.asyncio
async def test_sync_jobs_resume_processing_and_build_digest(tmp_path, monkeypatch):
    from sqlalchemy.orm import sessionmaker

    from app.db.models import ActivityItem, AuthorDigest, SocialPostProcessing
    from app.modules.jobs.repository import JobRepository
    from app.modules.jobs.worker import JobWorker
    from app.providers.registry import ProviderRegistry

    engine = create_engine(f"sqlite:///{tmp_path / 'pipeline.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    settings = Settings(
        social_agent_enabled=False,
        social_ocr_enabled=False,
        social_media_dir=tmp_path,
        social_media_cache_max_bytes=0,
    )
    now = datetime.now(UTC)

    async def batch(self, handle, since_id, limit, cursor=None, stop_before=None):
        return CollectorBatch(
            posts=[CollectorPost(id="5", text="原稿作業中", url="https://x.test/5", posted_at=now)],
            complete=True,
        )

    async def profile(*args, **kwargs):
        return None

    monkeypatch.setattr("app.modules.jobs.worker.SessionLocal", factory)
    monkeypatch.setattr("app.modules.social.service.XBrowserCollector.posts_batch", batch)
    monkeypatch.setattr("app.modules.social.service.XBrowserCollector.profile", profile)
    with factory() as session:
        author = Author(name="artist")
        session.add(author)
        session.flush()
        account = SocialAccount(author_id=author.id, handle="creator", status="confirmed")
        session.add(account)
        session.flush()
        account_id = account.id
        JobRepository(session).enqueue_social_sync(account_id)
        session.commit()
    normal = JobWorker(settings, ProviderRegistry([]), exclude_social_processing=True)
    processor = JobWorker(settings, ProviderRegistry([]), social_processing_only=True)
    assert await normal.run_once()
    with factory() as session:
        assert session.get(SocialAccount, account_id).last_post_id == "5"
        assert session.scalar(select(SocialPostProcessing)).status == "pending"
        assert session.scalar(select(ActivityItem)) is None
    assert await processor.run_once()
    with factory() as session:
        assert session.scalar(select(SocialPostProcessing)).status == "done"
        assert session.scalar(select(ActivityItem)) is not None
    assert await processor.run_once()
    with factory() as session:
        assert session.scalar(select(AuthorDigest)) is not None
