from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.base import Base
from app.db.models import Author, ReleaseSignal, SocialAccount, SocialPost
from app.modules.social.collector import CollectorPost
from app.modules.social.repository import SocialRepository
from app.modules.social.schemas import SocialAccountCreate
from app.modules.social.service import SocialSyncService


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as value:
        yield value


@pytest.mark.asyncio
async def test_ingest_does_not_auto_confirm_event_only(session: Session, tmp_path) -> None:
    author = Author(name="テスト作家")
    session.add(author)
    session.flush()
    account = SocialAccount(
        author_id=author.id, platform="x", handle="creator", status="confirmed"
    )
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
    result = await SocialSyncService(session, settings).ingest(account, incoming)
    signal = session.scalar(select(ReleaseSignal))
    assert result["signals"] == 1
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
    account = SocialAccount(
        author_id=author.id, platform="x", handle="creator", status="confirmed"
    )
    session.add(account)
    session.flush()
    settings = Settings(
        social_agent_enabled=False,
        social_ocr_enabled=False,
        social_media_dir=tmp_path,
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
    await SocialSyncService(session, settings).ingest(account, incoming)
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
    account = SocialAccount(
        author_id=author.id, platform="x", handle="creator", status="confirmed"
    )
    session.add(account)
    session.commit()
    account_id = account.id

    async def fake_posts(*args, **kwargs):
        return []

    async def fake_profile(*args, **kwargs):
        return None

    async def failing_ingest(service, current_account, incoming):
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

    monkeypatch.setattr("app.modules.social.service.XBrowserCollector.posts", fake_posts)
    monkeypatch.setattr("app.modules.social.service.XBrowserCollector.profile", fake_profile)
    monkeypatch.setattr(SocialSyncService, "ingest", failing_ingest)
    settings = Settings(
        social_agent_enabled=False,
        social_ocr_enabled=False,
        social_media_dir=tmp_path,
    )

    with pytest.raises(RuntimeError, match="摘要生成失败"):
        await SocialSyncService(session, settings).sync_account(account_id)

    partial = session.scalar(
        select(SocialPost).where(SocialPost.platform_post_id == "partial")
    )
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

    confirmed = repository.add_account(
        author.id, "creator", "circle", True, display_name="新名称"
    )

    assert confirmed.id == suggested.id
    assert confirmed.status == "confirmed"
    assert confirmed.account_type == "circle"
    assert confirmed.display_name == "新名称"
    assert confirmed.next_sync_at is not None


@pytest.mark.parametrize("handle", ["creator/name", "creator name", "x" * 16, "💥"])
def test_social_account_rejects_invalid_x_handles(handle: str) -> None:
    with pytest.raises(ValueError, match="X 账号格式不正确"):
        SocialAccountCreate(handle=handle)
