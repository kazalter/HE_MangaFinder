import hashlib
import hmac
import json
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
from app.modules.social.notifications import (
    NotificationService,
    QqBotClient,
    WebhookClient,
)
from app.modules.social.service import SocialSyncService


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as value:
        yield value


@pytest.mark.asyncio
async def test_webhook_client_signs_body_without_exposing_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

    class FakeClient:
        def __init__(self, **_: object) -> None:
            return None

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def post(
            self,
            url: str,
            *,
            content: bytes,
            headers: dict[str, str],
        ) -> FakeResponse:
            captured.update(url=url, content=content, headers=headers)
            return FakeResponse()

    monkeypatch.setattr("app.modules.social.notifications.httpx.AsyncClient", FakeClient)
    settings = Settings(
        webhook_enabled=True,
        webhook_url="https://hooks.example.test/mangafinder",
        webhook_secret="signing-secret",
    )

    await WebhookClient(settings).send(
        event="connection_test",
        idempotency_key="test-key",
        payload={"text": "测试消息"},
    )

    body = captured["content"]
    headers = captured["headers"]
    assert isinstance(body, bytes)
    assert isinstance(headers, dict)
    assert b"signing-secret" not in body
    timestamp = headers["X-MangaFinder-Timestamp"]
    expected = hmac.new(
        b"signing-secret",
        timestamp.encode() + b"." + body,
        hashlib.sha256,
    ).hexdigest()
    assert headers["X-MangaFinder-Signature"] == f"sha256={expected}"
    assert headers["Idempotency-Key"] == "test-key"
    assert json.loads(body)["data"] == {"text": "测试消息"}


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
async def test_signal_fans_out_and_is_marked_notified_after_delivery(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    qq_sent: list[str] = []
    webhook_sent: list[str] = []

    async def fake_qq_send(_: QqBotClient, content: str) -> None:
        qq_sent.append(content)

    async def fake_webhook_send(
        _: WebhookClient,
        *,
        event: str,
        idempotency_key: str,
        payload: dict[str, object],
    ) -> None:
        webhook_sent.append(f"{event}:{idempotency_key}:{payload['text']}")

    monkeypatch.setattr(QqBotClient, "send_text", fake_qq_send)
    monkeypatch.setattr(WebhookClient, "send", fake_webhook_send)
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
        webhook_enabled=True,
        webhook_url="https://hooks.example.test/mangafinder",
        webhook_secret="webhook-secret",
    )
    SocialSyncService(session, settings).queue_notification(signal, account, post)
    session.commit()

    assert signal.notified_at is None
    outbox = list(session.scalars(select(NotificationOutbox)))
    assert {item.channel for item in outbox} == {"qq", "webhook"}

    result = await NotificationService(session, settings).deliver_pending()

    assert result["delivered"] == 2
    assert qq_sent
    assert webhook_sent and webhook_sent[0].startswith("signal_created:webhook:signal:")
    assert signal.notified_at is not None


@pytest.mark.asyncio
async def test_digest_retries_only_the_channel_that_failed(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    qq_attempts = 0
    webhook_attempts = 0

    async def flaky_qq(_: QqBotClient, _content: str) -> None:
        nonlocal qq_attempts
        qq_attempts += 1
        if qq_attempts == 1:
            raise RuntimeError("temporary QQ failure")

    async def successful_webhook(
        _: WebhookClient,
        *,
        event: str,
        idempotency_key: str,
        payload: dict[str, object],
    ) -> None:
        nonlocal webhook_attempts
        webhook_attempts += 1
        assert event == "daily_digest"
        assert idempotency_key.startswith("webhook:daily_digest:")
        assert payload["text"] == "日报内容"

    monkeypatch.setattr(QqBotClient, "send_text", flaky_qq)
    monkeypatch.setattr(WebhookClient, "send", successful_webhook)
    delivery = DailyDigestDelivery(
        local_date="2026-07-17",
        timezone="Asia/Shanghai",
        period_start=datetime.now(UTC) - timedelta(days=1),
        period_end=datetime.now(UTC),
        author_ids=[1],
        content_hash="multi-channel-digest",
        payload={"text": "日报内容"},
        status="pending",
        next_attempt_at=datetime.now(UTC),
    )
    session.add(delivery)
    session.commit()
    settings = Settings(
        qq_bot_enabled=True,
        qq_bot_app_id="app",
        qq_bot_client_secret="secret",
        qq_bot_user_openid="openid",
        webhook_enabled=True,
        webhook_url="https://hooks.example.test/mangafinder",
        webhook_secret="webhook-secret",
    )
    service = NotificationService(session, settings)

    first = await service.deliver_pending()
    assert first["failed"] == 1
    assert delivery.status == "pending"
    assert delivery.payload["_delivered_channels"] == ["webhook"]
    assert webhook_attempts == 1

    delivery.next_attempt_at = datetime.now(UTC)
    session.commit()
    second = await service.deliver_pending()

    assert second["delivered"] == 1
    assert delivery.status == "delivered"
    assert qq_attempts == 2
    assert webhook_attempts == 1


def test_daily_digest_waits_for_new_posts_but_not_historical_ocr(session):
    from app.db.models import SocialPostProcessing

    now = datetime.now(UTC).replace(hour=22)
    settings = Settings(social_daily_digest_enabled=True, social_daily_digest_hour=0)
    add_activity(session, "artist", "creator", "9100", "新刊", now)
    post = session.scalar(select(SocialPost).where(SocialPost.platform_post_id == "9100"))
    state = SocialPostProcessing(
        post_id=post.id,
        content_hash=post.content_hash,
        status="pending",
        ocr_status="pending",
        backfill_only=False,
    )
    session.add(state)
    session.flush()
    assert not DailyDigestService(session, settings).is_due(now)
    state.backfill_only = True
    session.flush()
    assert DailyDigestService(session, settings).is_due(now)


@pytest.mark.asyncio
async def test_daily_digest_uses_period_llm_when_rolling_digest_misses_or_rates_low(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.modules.social.digest import ActivityDigestReviewer
    from app.modules.social.schemas import ActivityDigestVerdict

    now = datetime(2026, 7, 16, 12, 30, tzinfo=UTC)
    add_activity(
        session,
        "作者丙",
        "creator_c",
        "301",
        "C108の新刊サンプルを公開しました！",
        now - timedelta(hours=2),
    )
    post = session.scalar(select(SocialPost).where(SocialPost.platform_post_id == "301"))
    assert post is not None
    calls: list[str] = []

    async def fake_review(self, evidence):
        calls.append(str(evidence.get("digest_scope") or "rolling_7d"))
        if evidence.get("digest_scope") == "daily_brief":
            return ActivityDigestVerdict.model_validate(
                {
                    "summary": "作者公开了 C108 新刊样品。",
                    "highlights": [
                        {
                            "text": "公开了 C108 新作同人志样品",
                            "category": "release",
                            "importance": "low",
                            "factuality": "fact",
                            "post_ids": [post.id],
                        }
                    ],
                    "uncertainties": [],
                }
            )
        return ActivityDigestVerdict.model_validate(
            {
                "summary": "近7天有旧动态。",
                "highlights": [
                    {
                        "text": "此前发布的日常动态",
                        "category": "other",
                        "importance": "low",
                        "factuality": "fact",
                        "post_ids": [post.id],
                    }
                ],
                "uncertainties": [],
            }
        )

    monkeypatch.setattr(ActivityDigestReviewer, "review", fake_review)
    settings = Settings(
        agent_enabled=True,
        agent_provider="deepseek",
        agent_model="deepseek-chat",
        agent_api_key="test",
        social_agent_enabled=True,
        social_daily_digest_enabled=True,
        social_daily_digest_min_importance="normal",
    )

    result = await DailyDigestService(session, settings).build(force=True, now=now)
    delivery = session.scalar(select(DailyDigestDelivery))

    assert result["created"] is True
    assert calls == ["rolling_7d", "daily_brief"]
    assert delivery is not None
    assert "公开了 C108 新作同人志样品" in delivery.payload["text"]
    assert "公開しました" not in delivery.payload["text"]


def test_daily_digest_message_truncation_avoids_orphan_author_headers(session: Session) -> None:
    service = DailyDigestService(
        session,
        Settings(public_base_url="http://mangafinder.local"),
    )
    long_item = "发布了新作同人志进度更新与样品预览图" * 28
    sections = [
        (1, "作者一", [long_item, long_item, long_item]),
        (2, "作者二", ["更新了夏コミ摊位信息与新作同人志详细发售情报" * 8]),
    ]

    message = service._message("2026-07-16", sections)

    assert len(message) <= 1900
    assert "作者一" in message
    assert "作者二" not in message
    assert "共汇总 1 位作者、3 项近况" in message
    assert "未展开" in message


def test_daily_digest_fallback_formats_chinese_category_and_keeps_short_excerpt(
    session: Session,
) -> None:
    from app.db.models import ActivityItem

    service = DailyDigestService(session, Settings())
    item = ActivityItem(
        author_id=1,
        primary_post_id=1,
        cluster_key="k1",
        category="creation_progress",
        headline="原稿進捗です https://t.co/abc123 " + "とても長い日本語の文章" * 10,
        summary="原稿進捗です https://t.co/abc123 " + "とても長い日本語の文章" * 10,
        importance="normal",
        confidence=0.8,
        started_at=datetime.now(UTC),
        ended_at=datetime.now(UTC),
    )

    lines = service._activity_fallback([item])

    assert len(lines) == 1
    assert lines[0].startswith("【创作进度】")
    assert "创作稿件" in lines[0]
    assert "https://t.co/abc123" not in lines[0]
    assert lines[0].endswith("…")
    assert len(lines[0]) <= 85
