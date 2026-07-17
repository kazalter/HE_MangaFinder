from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.base import Base
from app.db.models import (
    Author,
    DailyDigestDelivery,
    NotificationOutbox,
    ReleaseSignal,
    SocialAccount,
    SocialPost,
)
from app.modules.social.activity import ActivityService
from app.modules.social.daily_digest import DailyDigestService
from app.modules.social.notifications import NotificationService, QqBotClient
from app.modules.social.service import SocialSyncService


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as value:
        yield value


def add_activity(
    session: Session, author_name: str, handle: str, post_id: str, text: str, posted_at: datetime
) -> None:
    author = Author(name=author_name)
    session.add(author)
    session.flush()
    account = SocialAccount(author_id=author.id, handle=handle, status="confirmed")
    session.add(account)
    session.flush()
    post = SocialPost(
        account_id=account.id,
        platform_post_id=post_id,
        post_type="original",
        text=text,
        url=f"https://x.com/{handle}/status/{post_id}",
        conversation_id=post_id,
        media=[],
        links=[],
        raw_metadata={},
        content_hash=post_id,
        posted_at=posted_at,
    )
    session.add(post)
    session.flush()
    ActivityService(session).ingest(account, post)


@pytest.mark.asyncio
async def test_daily_digest_aggregates_authors_and_is_idempotent(session: Session) -> None:
    now = datetime(2026, 7, 16, 12, 30, tzinfo=UTC)
    add_activity(
        session,
        "作者甲",
        "creator_a",
        "100",
        "夏コミ新刊の予約開始",
        now - timedelta(hours=2),
    )
    add_activity(session, "作者乙", "creator_b", "200", "原稿作業中です", now - timedelta(hours=1))
    settings = Settings(
        social_agent_enabled=False,
        social_daily_digest_enabled=True,
        social_daily_digest_timezone="Asia/Shanghai",
        social_daily_digest_hour=20,
        public_base_url="http://mangafinder.local",
    )
    service = DailyDigestService(session, settings)

    first = await service.build(force=True, now=now)
    second = await service.build(force=True, now=now + timedelta(minutes=1))

    delivery = session.scalar(select(DailyDigestDelivery))
    assert delivery is not None
    assert first["created"] is True
    assert second["created"] is False
    assert delivery.status == "pending"
    assert delivery.local_date == "2026-07-16"
    assert "作者甲" in delivery.payload["text"]
    assert "作者乙" in delivery.payload["text"]
    assert "纯转推已排除" in delivery.payload["text"]
    assert session.scalar(select(func.count(DailyDigestDelivery.id))) == 1


@pytest.mark.asyncio
async def test_first_daily_digest_uses_rolling_baseline(session: Session) -> None:
    now = datetime(2026, 7, 16, 6, 30, tzinfo=UTC)
    add_activity(
        session,
        "作者甲",
        "creator_a",
        "baseline-100",
        "夏コミ新刊の通販を開始しました",
        now - timedelta(days=3),
    )
    settings = Settings(
        social_agent_enabled=False,
        social_daily_digest_enabled=True,
        social_daily_digest_initial_lookback_days=7,
    )

    result = await DailyDigestService(session, settings).build(force=True, now=now)
    delivery = session.scalar(select(DailyDigestDelivery))

    assert result["created"] is True
    assert delivery is not None
    assert delivery.period_start.replace(tzinfo=UTC) == now - timedelta(days=7)
    assert "作者甲" in delivery.payload["text"]


def test_daily_digest_is_due_once_after_configured_local_hour(session: Session) -> None:
    settings = Settings(
        social_daily_digest_enabled=True,
        social_daily_digest_timezone="Asia/Shanghai",
        social_daily_digest_hour=20,
    )
    service = DailyDigestService(session, settings)

    assert service.is_due(datetime(2026, 7, 16, 11, 59, tzinfo=UTC)) is False
    assert service.is_due(datetime(2026, 7, 16, 12, 0, tzinfo=UTC)) is True

    session.add(
        DailyDigestDelivery(
            local_date="2026-07-16",
            timezone="Asia/Shanghai",
            period_start=datetime(2026, 7, 15, 16, tzinfo=UTC),
            period_end=datetime(2026, 7, 16, 12, tzinfo=UTC),
            author_ids=[],
            content_hash="empty",
            payload={},
            status="skipped",
        )
    )
    session.commit()
    assert service.is_due(datetime(2026, 7, 16, 13, 0, tzinfo=UTC)) is False


@pytest.mark.asyncio
async def test_notification_service_delivers_daily_digest(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[str] = []

    async def fake_send(_: QqBotClient, content: str) -> None:
        sent.append(content)

    monkeypatch.setattr(QqBotClient, "send_text", fake_send)
    session.add(
        DailyDigestDelivery(
            local_date="2026-07-16",
            timezone="Asia/Shanghai",
            period_start=datetime.now(UTC) - timedelta(days=1),
            period_end=datetime.now(UTC),
            author_ids=[1, 2],
            content_hash="digest-hash",
            payload={"text": "【MangaFinder · 作者近况日报】"},
            status="pending",
            next_attempt_at=datetime.now(UTC),
        )
    )
    session.commit()
    settings = Settings(
        qq_bot_enabled=True,
        qq_bot_app_id="app",
        qq_bot_client_secret="secret",
        qq_bot_user_openid="openid",
    )

    result = await NotificationService(session, settings).deliver_pending()
    delivery = session.scalar(select(DailyDigestDelivery))

    assert result["digest_processed"] == 1
    assert result["delivered"] == 1
    assert sent == ["【MangaFinder · 作者近况日报】"]
    assert delivery is not None and delivery.status == "delivered"


@pytest.mark.asyncio
async def test_signal_is_marked_notified_only_after_delivery(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[str] = []

    async def fake_send(_: QqBotClient, content: str) -> None:
        sent.append(content)

    monkeypatch.setattr(QqBotClient, "send_text", fake_send)
    author = Author(name="作者甲")
    session.add(author)
    session.flush()
    account = SocialAccount(author_id=author.id, handle="creator", status="confirmed")
    session.add(account)
    session.flush()
    post = SocialPost(
        account_id=account.id,
        platform_post_id="notification-1",
        post_type="original",
        text="新刊予約開始",
        url="https://x.com/creator/status/notification-1",
        conversation_id="notification-1",
        media=[],
        links=[],
        raw_metadata={},
        content_hash="notification-1",
        posted_at=datetime.now(UTC),
    )
    session.add(post)
    session.flush()
    signal = ReleaseSignal(
        author_id=author.id,
        primary_post_id=post.id,
        cluster_key="notification-1",
        kind="new_release",
        confidence=0.95,
        status="confirmed",
        evidence=["新刊"],
        counter_evidence=[],
        missing_information=[],
    )
    session.add(signal)
    session.flush()
    settings = Settings(
        qq_bot_enabled=True,
        qq_bot_app_id="app",
        qq_bot_client_secret="secret",
        qq_bot_user_openid="openid",
    )
    SocialSyncService(session, settings).queue_notification(signal, account, post)
    session.commit()

    assert signal.notified_at is None
    assert session.scalar(select(NotificationOutbox)) is not None

    result = await NotificationService(session, settings).deliver_pending()

    assert result["delivered"] == 1
    assert sent
    assert signal.notified_at is not None
