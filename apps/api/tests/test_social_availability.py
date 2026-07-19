from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.base import Base
from app.db.models import Author, SocialAccount, SocialPost
from app.modules.social.availability import PostAvailabilityService


@pytest.mark.asyncio
async def test_deleted_post_requires_a_delayed_second_confirmation(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        author = Author(name="作家")
        session.add(author)
        session.flush()
        account = SocialAccount(author_id=author.id, handle="creator", status="confirmed")
        session.add(account)
        session.flush()
        post = SocialPost(
            account_id=account.id,
            platform_post_id="123",
            text="保留的动态",
            url="https://x.com/creator/status/123",
            media=[],
            links=[],
            raw_metadata={},
            content_hash="123",
            posted_at=datetime.now(UTC),
        )
        session.add(post)
        session.flush()

        async def deleted(*_: object, **__: object) -> dict[str, str]:
            return {"status": "deleted", "reason": "Post was deleted by the Post author"}

        monkeypatch.setattr(
            "app.modules.social.availability.XBrowserCollector.post_status", deleted
        )
        settings = Settings(
            social_post_delete_confirm_hours=24,
            social_media_dir=tmp_path,
            _env_file=None,
        )
        service = PostAvailabilityService(session, settings)

        await service.verify(post)
        assert post.availability_status == "unavailable"
        assert post.availability_reason.startswith("deleted_candidate:")

        post.unavailable_since = datetime.now(UTC) - timedelta(hours=25)
        await service.verify(post)
        assert post.availability_status == "deleted"
        assert post.text == "保留的动态"
