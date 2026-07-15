from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.base import Base
from app.db.models import Author, ReleaseSignal, SocialAccount
from app.modules.social.collector import CollectorPost
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
