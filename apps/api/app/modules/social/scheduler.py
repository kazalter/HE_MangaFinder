import asyncio
import logging
from datetime import UTC, datetime

from app.core.config import Settings
from app.db.session import SessionLocal
from app.modules.jobs.repository import JobRepository
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
            jobs.enqueue_notification_delivery_if_needed()
            session.commit()

