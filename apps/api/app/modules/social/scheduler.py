import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select

from app.core.config import Settings
from app.db.models import AuthorDigest, SocialPost, SocialPostProcessing
from app.db.session import SessionLocal
from app.modules.jobs.repository import JobRepository
from app.modules.social.daily_digest import DailyDigestService
from app.modules.social.repository import SocialRepository

logger = logging.getLogger(__name__)


class SocialScheduler:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def run(self) -> None:
        while True:
            try:
                self.enqueue_due()
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Social scheduler failed")
                await asyncio.sleep(60)

    def enqueue_due(self) -> None:
        with SessionLocal() as session:
            jobs = JobRepository(session)
            for account in SocialRepository(session).due_accounts(datetime.now(UTC)):
                jobs.enqueue_social_sync(account.id)
            if self.settings.social_ocr_enabled:
                now = datetime.now(UTC)
                candidates = session.execute(
                    select(SocialPost, SocialPostProcessing)
                    .outerjoin(SocialPostProcessing, SocialPostProcessing.post_id == SocialPost.id)
                    .where(
                        SocialPost.post_type != "retweet",
                        or_(
                            (SocialPostProcessing.post_id.is_(None))
                            & or_(SocialPost.ocr_text.is_(None), SocialPost.ocr_text == ""),
                            (SocialPostProcessing.status == "done")
                            & (SocialPostProcessing.ocr_status.in_(["retry", "disabled"]))
                            & (SocialPostProcessing.updated_at <= now - timedelta(hours=1)),
                            (SocialPostProcessing.status == "retry")
                            & (SocialPostProcessing.updated_at <= now - timedelta(hours=1)),
                        ),
                    )
                    .order_by(SocialPost.posted_at.desc())
                )
                queued = 0
                for post, state in candidates:
                    if not any(
                        item.get("type") == "image" and item.get("url")
                        for item in (post.media or [])
                        if isinstance(item, dict)
                    ):
                        continue
                    if state is None and post.ocr_text:
                        continue
                    if state is None:
                        state = SocialPostProcessing(
                            post_id=post.id,
                            content_hash=post.content_hash,
                            status="pending",
                            ocr_status="pending",
                            backfill_only=True,
                        )
                        session.add(state)
                    else:
                        if state.status == "done":
                            state.backfill_only = True
                        state.status = "pending"
                    jobs.enqueue_social_post(post.id)
                    queued += 1
                    if queued >= 20:
                        break
            if self.settings.social_agent_configured:
                now = datetime.now(UTC)
                retry_authors = session.scalars(
                    select(AuthorDigest.author_id)
                    .where(
                        AuthorDigest.generated_by == "rules",
                        AuthorDigest.period_end > now,
                        AuthorDigest.updated_at <= now - timedelta(minutes=15),
                    )
                    .distinct()
                )
                for author_id in retry_authors:
                    jobs.enqueue_social_digest(author_id)
            if DailyDigestService(session, self.settings).is_due(datetime.now(UTC)):
                jobs.enqueue_daily_digest()
            jobs.enqueue_notification_delivery_if_needed()
            session.commit()
