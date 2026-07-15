import asyncio
import logging

from app.core.config import Settings
from app.db.session import SessionLocal
from app.modules.agent_review.service import AgentReviewService
from app.modules.catalog.cover_maintenance import CoverFingerprintRefreshService
from app.modules.catalog.downloads import DownloadService
from app.modules.catalog.service import DiscoveryService
from app.modules.jobs.repository import (
    AGENT_REVIEW_SUGGESTIONS,
    DELIVER_NOTIFICATIONS,
    DISCOVER_AUTHOR,
    DOWNLOAD_CHAPTER,
    REFRESH_COVER_FINGERPRINTS,
    SOCIAL_SYNC_ACCOUNT,
    JobRepository,
)
from app.modules.social.notifications import NotificationService
from app.modules.social.service import SocialSyncService
from app.providers.registry import ProviderRegistry

logger = logging.getLogger(__name__)


class JobWorker:
    def __init__(self, settings: Settings, providers: ProviderRegistry) -> None:
        self.settings = settings
        self.providers = providers

    async def run(self) -> None:
        while True:
            try:
                processed = await self.run_once()
                if not processed:
                    await asyncio.sleep(self.settings.poll_interval_seconds)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Background worker loop failed")
                await asyncio.sleep(self.settings.poll_interval_seconds)

    async def run_once(self) -> bool:
        with SessionLocal() as session:
            jobs = JobRepository(session)
            job = jobs.claim_next()
            if job is None:
                return False
            try:
                if job.kind == DISCOVER_AUTHOR:
                    await DiscoveryService(session, self.providers).discover_author(
                        int(job.payload["author_id"])
                    )
                    if (
                        self.settings.agent_review_after_discovery
                        and self.settings.agent_configured
                    ):
                        jobs.enqueue_agent_reviews()
                elif job.kind == DOWNLOAD_CHAPTER:
                    path = await DownloadService(
                        session, self.providers, self.settings
                    ).download(
                        int(job.payload["work_id"]),
                        str(job.payload["provider"]),
                        str(job.payload["chapter_id"]),
                    )
                    job.payload = {**job.payload, "output_path": path}
                elif job.kind == REFRESH_COVER_FINGERPRINTS:
                    result = await CoverFingerprintRefreshService(
                        session, self.providers
                    ).run(
                        bool(job.payload.get("force", False))
                    )
                    job.payload = {**job.payload, **result}
                elif job.kind == AGENT_REVIEW_SUGGESTIONS:
                    result = await AgentReviewService(
                        session, self.settings
                    ).run_pending(job.payload.get("max_reviews"))
                    job.payload = {**job.payload, **result}
                elif job.kind == SOCIAL_SYNC_ACCOUNT:
                    result = await SocialSyncService(
                        session, self.settings
                    ).sync_account(int(job.payload["account_id"]))
                    job.payload = {**job.payload, **result}
                    jobs.enqueue_notification_delivery_if_needed()
                elif job.kind == DELIVER_NOTIFICATIONS:
                    result = await NotificationService(
                        session, self.settings
                    ).deliver_pending()
                    job.payload = {**job.payload, **result}
                else:
                    raise ValueError(f"未知任务类型: {job.kind}")
                jobs.succeed(job)
                if job.kind == REFRESH_COVER_FINGERPRINTS:
                    if int(job.payload.get("remaining", 0)) > 0:
                        jobs.enqueue_cover_fingerprint_refresh()
                        session.commit()
                    elif self.settings.agent_configured:
                        jobs.enqueue_agent_reviews()
                        session.commit()
            except Exception as exc:
                logger.warning("Job %s failed: %s", job.id, exc)
                jobs.fail(job, str(exc), self.settings.max_job_attempts)
            return True
