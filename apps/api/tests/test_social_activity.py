from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.base import Base
from app.db.models import Author, SocialAccount, SocialPost
from app.modules.social.activity import ActivityService, assess_activity
from app.modules.social.digest import DigestService
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
        posted_at=datetime.now(UTC) - timedelta(hours=1),
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
    post = make_post(
        session, account.id, "1", "RT @other: 新刊です", post_type="retweet"
    )

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
    digest = await DigestService(
        session, Settings(social_agent_enabled=False)
    ).refresh(author.id)

    assert digest is not None
    assert digest.generated_by == "rules"
    assert digest.evidence_post_ids == [original.id]
    assert all(original.id in item["post_ids"] for item in digest.highlights)
