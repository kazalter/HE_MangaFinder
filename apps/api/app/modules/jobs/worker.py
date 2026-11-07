import asyncio
import logging
import threading
from dataclasses import asdict

from sqlalchemy.exc import OperationalError

from app.core.config import Settings
from app.db.models import Job, JobStatus, SocialPostProcessing
from app.db.session import SessionLocal
from app.modules.agent_review.service import AgentReviewService
from app.modules.catalog.cover_maintenance import CoverFingerprintRefreshService
from app.modules.catalog.downloads import DownloadService
from app.modules.catalog.service import DiscoveryService
from app.modules.jobs.repository import (
    AGENT_REVIEW_SUGGESTIONS,
    BUILD_DAILY_DIGEST,
    DELIVER_NOTIFICATIONS,
    DISCOVER_AUTHOR,
    DOWNLOAD_CHAPTER,
    REFRESH_COVER_FINGERPRINTS,
    REFRESH_SOCIAL_DIGEST,
    SOCIAL_PROCESS_POST,
    SOCIAL_SYNC_ACCOUNT,
    JobRepository,
)
from app.modules.social.daily_digest import DailyDigestService
from app.modules.social.digest import DigestService
from app.modules.social.notifications import NotificationService
from app.modules.social.service import SocialSyncService
from app.providers.registry import ProviderRegistry, build_registry

logger = logging.getLogger(__name__)


class JobWorker:
    def __init__(
        self,
        settings: Settings,
        providers: ProviderRegistry,
        *,
        exclude_downloads: bool = False,
        exclude_covers: bool = False,
        covers_only: bool = False,
        exclude_notifications: bool = False,
        notifications_only: bool = False,
        exclude_social_processing: bool = False,
        social_processing_only: bool = False,
    ) -> None:
        self.settings = settings
        self.providers = providers
        self.exclude_downloads = exclude_downloads
        self.exclude_covers = exclude_covers
        self.covers_only = covers_only
        self.exclude_notifications = exclude_notifications
        self.notifications_only = notifications_only
        self.exclude_social_processing = exclude_social_processing
        self.social_processing_only = social_processing_only

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
            job = jobs.claim_next(
                kinds=(
                    {REFRESH_COVER_FINGERPRINTS}
                    if self.covers_only
                    else {DELIVER_NOTIFICATIONS}
                    if self.notifications_only
                    else {SOCIAL_PROCESS_POST, REFRESH_SOCIAL_DIGEST}
                    if self.social_processing_only
                    else None
                ),
                exclude_kinds=({DOWNLOAD_CHAPTER} if self.exclude_downloads else set())
                | ({REFRESH_COVER_FINGERPRINTS} if self.exclude_covers else set())
                | ({DELIVER_NOTIFICATIONS} if self.exclude_notifications else set())
                | (
                    {SOCIAL_PROCESS_POST, REFRESH_SOCIAL_DIGEST}
                    if self.exclude_social_processing
                    else set()
                ),
                prioritize_notifications=self.exclude_downloads,
            )
            if job is None:
                return False
            try:
                if job.kind == DISCOVER_AUTHOR:
                    discovery = DiscoveryService(session, self.providers)
                    trigger = str(job.payload.get("trigger", "manual"))
                    if trigger == "manual":
                        result = await discovery.discover_author(int(job.payload["author_id"]))
                    else:
                        provider_names = {
                            str(provider) for provider in job.payload.get("providers", [])
                        }
                        result = await discovery.discover_author(
                            int(job.payload["author_id"]),
                            trigger=trigger,
                            provider_names=(
                                provider_names if trigger == "provider_retry" else None
                            ),
                        )
                    job.payload = {**job.payload, **result}
                    if (
                        self.settings.agent_review_after_discovery
                        and self.settings.agent_configured
                    ):
                        jobs.enqueue_agent_reviews()
                elif job.kind == DOWNLOAD_CHAPTER:
                    path, verification = await DownloadService(
                        session, self.providers, self.settings
                    ).download(
                        int(job.payload["work_id"]),
                        str(job.payload["provider"]),
                        str(job.payload["chapter_id"]),
                    )
                    job.payload = {
                        **job.payload,
                        "output_path": path,
                        "verification": asdict(verification),
                    }
                elif job.kind == REFRESH_COVER_FINGERPRINTS:
                    result = await CoverFingerprintRefreshService(session, self.providers).run(
                        bool(job.payload.get("force", False))
                    )
                    job.payload = {**job.payload, **result}
                elif job.kind == AGENT_REVIEW_SUGGESTIONS:
                    result = await AgentReviewService(session, self.settings).run_pending(
                        job.payload.get("max_reviews")
                    )
                    job.payload = {**job.payload, **result}
                elif job.kind == SOCIAL_SYNC_ACCOUNT:
                    result = await SocialSyncService(session, self.settings).sync_account(
                        int(job.payload["account_id"])
                    )
                    job.payload = {**job.payload, **result}
                    jobs.enqueue_notification_delivery_if_needed()
                elif job.kind == SOCIAL_PROCESS_POST:
                    result = await SocialSyncService(session, self.settings).process_post(
                        int(job.payload["post_id"])
                    )
                    job.payload = {**job.payload, **result}
                    jobs.enqueue_notification_delivery_if_needed()
                elif job.kind == REFRESH_SOCIAL_DIGEST:
                    digest = await DigestService(session, self.settings).refresh(
                        int(job.payload["author_id"])
                    )
                    job.payload = {**job.payload, "digest_id": digest.id if digest else None}
                elif job.kind == BUILD_DAILY_DIGEST:
                    result = await DailyDigestService(session, self.settings).build(
                        force=bool(job.payload.get("force", False))
                    )
                    job.payload = {**job.payload, **result}
                    jobs.enqueue_notification_delivery_if_needed()
                elif job.kind == DELIVER_NOTIFICATIONS:
                    result = await NotificationService(session, self.settings).deliver_pending()
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
                job_id = job.id
                session.rollback()
                job = session.get(type(job), job_id)
                if job is None:
                    raise RuntimeError(f"任务 {job_id} 在失败处理期间被删除") from exc
                if self.covers_only or self.notifications_only or self.social_processing_only:
                    while True:
                        try:
                            if job.status == JobStatus.RUNNING:
                                if (
                                    job.kind == SOCIAL_PROCESS_POST
                                    and job.attempts >= self.settings.max_job_attempts
                                ):
                                    state = session.get(
                                        SocialPostProcessing, int(job.payload["post_id"])
                                    )
                                    if state:
                                        state.status = "retry"
                                        state.last_error = str(exc)[:1000]
                                jobs.fail(job, str(exc), self.settings.max_job_attempts)
                            break
                        except OperationalError as lock_error:
                            if not any(
                                word in str(lock_error).lower() for word in ("locked", "busy")
                            ):
                                raise
                            session.rollback()
                            await asyncio.sleep(max(0.1, self.settings.poll_interval_seconds))
                            job = session.get(Job, job_id)
                            if job is None:
                                break
                else:
                    jobs.fail(job, str(exc), self.settings.max_job_attempts)
            return True


class CoverWorkerThread:
    """Keep indexing and its synchronous database work off the API event loop."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name="cover-index", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def _make_worker(self, providers: ProviderRegistry) -> JobWorker:
        return JobWorker(self.settings, providers, covers_only=True)

    def _run(self) -> None:
        async def serve() -> None:
            providers = build_registry(self.settings)
            worker = self._make_worker(providers)
            try:
                while not self.stop_event.is_set():
                    try:
                        processed = await worker.run_once()
                        if not processed:
                            await asyncio.sleep(self.settings.poll_interval_seconds)
                    except Exception:
                        logger.exception(
                            "Background lane %s failed", threading.current_thread().name
                        )
                        await asyncio.sleep(self.settings.poll_interval_seconds)
            finally:
                await providers.close()

        asyncio.run(serve())

    async def stop(self) -> None:
        self.stop_event.set()
        await asyncio.to_thread(self.thread.join, 5)
        if self.thread.is_alive():
            logger.warning(
                "%s still stopping; interrupted jobs recover on restart", self.thread.name
            )


class NotificationWorkerThread(CoverWorkerThread):
    """Deliver notifications independently of long-running discovery jobs."""

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.thread.name = "notification-delivery"

    def _make_worker(self, providers: ProviderRegistry) -> JobWorker:
        return JobWorker(self.settings, providers, notifications_only=True)


class SocialProcessingWorkerThread(CoverWorkerThread):
    """Perform OCR and model work without blocking discovery or notification queues."""

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.thread.name = "social-processing"

    def _make_worker(self, providers: ProviderRegistry) -> JobWorker:
        return JobWorker(self.settings, providers, social_processing_only=True)


class DownloadWorker:
    """One download lane; network and archive work do not hold up other jobs."""

    def __init__(self, settings: Settings, providers: ProviderRegistry) -> None:
        self.settings = settings
        self.providers = providers

    @staticmethod
    def _claim() -> tuple[int, dict] | None:
        with SessionLocal() as session:
            job = JobRepository(session).claim_next(kinds={DOWNLOAD_CHAPTER})
            return (job.id, dict(job.payload)) if job else None

    def _finish(self, job_id: int, payload: dict, error: str | None) -> None:
        with SessionLocal() as session:
            job = session.get(Job, job_id)
            if job is None or job.status != JobStatus.RUNNING:
                return
            jobs = JobRepository(session)
            if error is None:
                job.payload = payload
                jobs.succeed(job)
            else:
                jobs.fail(job, error, self.settings.max_job_attempts)

    async def _finish_with_retry(self, job_id: int, payload: dict, error: str | None) -> None:
        while True:
            try:
                await asyncio.to_thread(self._finish, job_id, payload, error)
                return
            except OperationalError as exc:
                if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                    raise
                await asyncio.sleep(max(0.1, self.settings.poll_interval_seconds))

    async def run_once(self) -> bool:
        claimed = await asyncio.to_thread(self._claim)
        if claimed is None:
            return False
        job_id, payload = claimed
        error = None
        try:
            with SessionLocal() as session:
                path, verification = await DownloadService(
                    session, self.providers, self.settings
                ).download(
                    int(payload["work_id"]), str(payload["provider"]), str(payload["chapter_id"])
                )
                payload = {**payload, "output_path": path, "verification": asdict(verification)}
        except Exception as exc:
            logger.warning("Download job %s failed: %s", job_id, exc)
            error = str(exc)
        await self._finish_with_retry(job_id, payload, error)
        return True

    async def run(self) -> None:
        while True:
            try:
                if not await self.run_once():
                    await asyncio.sleep(self.settings.poll_interval_seconds)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Download worker loop failed")
                await asyncio.sleep(self.settings.poll_interval_seconds)
