from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.base import Base
from app.db.models import Author, SocialAccount, SocialPost
from app.modules.social.activity import ActivityService, assess_activity
from app.modules.social.digest import (
    DigestService,
    localize_known_terms,
    translation_hints,
)
from app.modules.social.repository import SocialRepository


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as value:
        yield value


def make_post(
    session: Session,
    account_id: int,
    post_id: str,
    text: str,
    *,
    post_type: str = "original",
    conversation_id: str | None = None,
    posted_at: datetime | None = None,
) -> SocialPost:
    post = SocialPost(
        account_id=account_id,
        platform_post_id=post_id,
        post_type=post_type,
        text=text,
        url=f"https://x.com/creator/status/{post_id}",
        conversation_id=conversation_id or post_id,
        media=[],
        links=[],
        raw_metadata={},
        content_hash=post_id,
        posted_at=posted_at or datetime.now(UTC) - timedelta(hours=1),
    )
    session.add(post)
    session.flush()
    return post


def test_retweet_is_kept_out_of_activity(session: Session) -> None:
    author = Author(name="作家")
    session.add(author)
    session.flush()
    account = SocialAccount(author_id=author.id, handle="creator", status="confirmed")
    session.add(account)
    session.flush()
    post = make_post(session, account.id, "1", "RT @other: 新刊です", post_type="retweet")

    assert assess_activity(post) is None
    assert ActivityService(session).ingest(account, post) is None
    assert SocialRepository(session).list_activities(author.id) == []


def test_self_thread_clusters_into_one_activity(session: Session) -> None:
    author = Author(name="作家")
    session.add(author)
    session.flush()
    account = SocialAccount(author_id=author.id, handle="creator", status="confirmed")
    session.add(account)
    session.flush()
    first = make_post(session, account.id, "10", "原稿作業中です", conversation_id="10")
    second = make_post(session, account.id, "11", "原稿の彩色まで進みました", conversation_id="10")

    service = ActivityService(session)
    service.ingest(account, first)
    service.ingest(account, second)

    rows = SocialRepository(session).list_activities(author.id)
    assert len(rows) == 1
    assert rows[0].category == "creation_progress"
    assert [post.id for post in SocialRepository(session).activity_posts(rows[0].id)] == [
        first.id,
        second.id,
    ]


@pytest.mark.asyncio
async def test_fallback_digest_is_grounded_in_non_retweets(session: Session) -> None:
    author = Author(name="作家")
    session.add(author)
    session.flush()
    account = SocialAccount(author_id=author.id, handle="creator", status="confirmed")
    session.add(account)
    session.flush()
    original = make_post(session, account.id, "20", "C108新刊の原稿作業中です")
    make_post(session, account.id, "21", "RT @other: 新刊です", post_type="retweet")

    ActivityService(session).ingest(account, original)
    digest = await DigestService(session, Settings(social_agent_enabled=False)).refresh(author.id)

    assert digest is not None
    assert digest.generated_by == "rules"
    assert digest.evidence_post_ids == [original.id]
    assert all(original.id in item["post_ids"] for item in digest.highlights)


@pytest.mark.asyncio
async def test_fallback_digest_sorts_mixed_sqlite_and_new_utc_datetimes(
    session: Session,
) -> None:
    author = Author(name="作家")
    session.add(author)
    session.flush()
    author_id = author.id
    account = SocialAccount(author_id=author_id, handle="creator", status="confirmed")
    session.add(account)
    session.flush()
    account_id = account.id
    old = make_post(
        session,
        account_id,
        "22",
        "原稿作業中です",
        posted_at=datetime.now(UTC) - timedelta(hours=2),
    )
    old_id = old.id
    session.commit()
    session.expunge_all()

    account = session.get(SocialAccount, account_id)
    assert account is not None
    old = session.get(SocialPost, old_id)
    assert old is not None
    assert old.posted_at.utcoffset() == timedelta(0)
    old.posted_at = old.posted_at.replace(tzinfo=None)
    new = make_post(
        session,
        account_id,
        "23",
        "原稿の彩色まで進みました",
        posted_at=datetime.now(UTC) - timedelta(hours=1),
    )

    digest = await DigestService(session, Settings(social_agent_enabled=False)).refresh(author_id)

    assert digest is not None
    assert digest.highlights[0]["post_ids"] == [new.id]
    assert any(item["post_ids"] == [old_id] for item in digest.highlights)


def test_translation_hints_and_fallback_terms_are_readable_chinese(session: Session) -> None:
    author = Author(name="作家")
    session.add(author)
    session.flush()
    account = SocialAccount(author_id=author.id, handle="creator", status="confirmed")
    session.add(account)
    session.flush()
    post = make_post(
        session,
        account.id,
        "30",
        "夏コミ用トレカケースアクキーのサンプル、通販は明日から",
    )

    hints = translation_hints([post])

    assert {item["source"] for item in hints} >= {
        "夏コミ",
        "トレカケースアクキー",
        "サンプル",
        "通販",
    }
    localized = localize_known_terms(post.text)
    assert "用于夏季" not in localized  # deterministic fallback translates terms, not grammar
    assert "夏季 Comic Market（夏コミ）" in localized
    assert "卡套亚克力挂件" in localized
    assert "样品" in localized
    assert "网售" in localized
    assert localize_known_terms("夏季 Comic Market（夏コミ）") == "夏季 Comic Market（夏コミ）"
    assert localize_known_terms("转发了一条花店照片") == "引用了一条花店照片"
    assert localize_known_terms("转发花店照片") == "引用花店照片"


@pytest.mark.asyncio
async def test_prompt_version_invalidates_digest_cache(session: Session) -> None:
    author = Author(name="作家")
    session.add(author)
    session.flush()
    account = SocialAccount(author_id=author.id, handle="creator", status="confirmed")
    session.add(account)
    session.flush()
    make_post(session, account.id, "40", "夏コミ新刊の予約開始")

    first = await DigestService(
        session,
        Settings(social_agent_enabled=False, social_agent_prompt_version="social-v1"),
    ).refresh(author.id)
    assert first is not None
    first_hash = first.content_hash

    second = await DigestService(
        session,
        Settings(social_agent_enabled=False, social_agent_prompt_version="social-zh-v2"),
    ).refresh(author.id)

    assert second is not None
    assert second.id == first.id
    assert second.content_hash != first_hash
    assert second.prompt_version == "social-zh-v2"


@pytest.mark.asyncio
async def test_failed_model_digest_retries_after_cooldown_and_manual_force(session, monkeypatch):
    from datetime import timedelta

    from app.modules.social.digest import ActivityDigestReviewer
    from app.modules.social.schemas import ActivityDigestVerdict

    author = Author(name="retry artist")
    session.add(author)
    session.flush()
    account = SocialAccount(author_id=author.id, handle="retry", status="confirmed")
    session.add(account)
    session.flush()
    post = make_post(session, account.id, "1000", "新刊の予約開始")
    now = datetime.now(UTC)
    calls = []

    async def review(self, evidence):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("temporary failure")
        return ActivityDigestVerdict.model_validate(
            {
                "summary": "新刊",
                "highlights": [
                    {
                        "text": "新刊",
                        "category": "release",
                        "importance": "high",
                        "factuality": "fact",
                        "post_ids": [post.id],
                    }
                ],
                "uncertainties": [],
            }
        )

    monkeypatch.setattr(ActivityDigestReviewer, "review", review)
    settings = Settings(
        social_agent_enabled=True,
        agent_enabled=True,
        agent_base_url="https://example.test",
        agent_model="model",
        agent_api_key="test",
    )
    service = DigestService(session, settings)
    first = await service.refresh(author.id, now=now)
    assert first.generated_by == "rules" and first.error
    session.flush()
    await service.refresh(author.id, now=now + timedelta(minutes=1))
    assert len(calls) == 1
    final = await service.refresh(author.id, now=now + timedelta(minutes=1), force=True)
    assert len(calls) == 2 and final.generated_by == "agent" and final.error is None


def test_digest_evidence_returns_only_the_tracked_authors_posts(session):
    from app.db.models import AuthorDigest
    from app.modules.social.router import _digest_read

    author = Author(name="artist")
    other = Author(name="other")
    session.add_all([author, other])
    session.flush()
    a = SocialAccount(author_id=author.id, handle="a", status="confirmed")
    b = SocialAccount(author_id=other.id, handle="b", status="confirmed")
    session.add_all([a, b])
    session.flush()
    allowed = make_post(session, a.id, "21", "original")
    unrelated = make_post(session, b.id, "22", "unrelated")
    now = datetime.now(UTC)
    digest = AuthorDigest(
        author_id=author.id,
        period_type="rolling_7d",
        period_start=now,
        period_end=now,
        summary="summary",
        highlights=[],
        uncertainties=[],
        evidence_post_ids=[allowed.id, unrelated.id],
        content_hash="x",
        generated_by="rules",
    )
    session.add(digest)
    session.flush()
    response = _digest_read(session, digest)
    assert [post.id for post in response.evidence_posts] == [allowed.id]
    assert response.evidence_posts[0].url.endswith("/21")
