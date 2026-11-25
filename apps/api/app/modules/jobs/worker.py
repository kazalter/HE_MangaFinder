import asyncio
import logging
import threading
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.observability import capture_metrics, record_metric
from app.core.time import as_utc
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
_SQLITE_LOCK_RETRY_ATTEMPTS = 10


def _is_sqlite_lock_error(exc: BaseException) -> bool:
    if not isinstance(exc, OperationalError):
        return False
    message = str(exc).lower()
    return "locked" in message or "busy" in message


def _should_retry_sqlite_lock(exc: OperationalError, attempt: int) -> bool:
    return _is_sqlite_lock_error(exc) and attempt < _SQLITE_LOCK_RETRY_ATTEMPTS - 1


def _sqlite_retry_delay(poll_interval_seconds: float, attempt: int) -> float:
    base = min(0.5, max(0.05, poll_interval_seconds * 0.25))
    return min(1.0, base * (attempt + 1))


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

    def _claim_args(self) -> dict[str, Any]:
        return {
            "kinds": (
                {REFRESH_COVER_FINGERPRINTS}
                if self.covers_only
                else {DELIVER_NOTIFICATIONS}
                if self.notifications_only
                else {SOCIAL_PROCESS_POST, REFRESH_SOCIAL_DIGEST}
                if self.social_processing_only
                else None
            ),
            "exclude_kinds": ({DOWNLOAD_CHAPTER} if self.exclude_downloads else set())
            | ({REFRESH_COVER_FINGERPRINTS} if self.exclude_covers else set())
            | ({DELIVER_NOTIFICATIONS} if self.exclude_notifications else set())
            | (
                {SOCIAL_PROCESS_POST, REFRESH_SOCIAL_DIGEST}
                if self.exclude_social_processing
                else set()
            ),
            "prioritize_notifications": self.exclude_downloads,
        }

    async def _sleep_before_retry(self, attempt: int) -> None:
        delay = _sqlite_retry_delay(self.settings.poll_interval_seconds, attempt)
        await asyncio.sleep(delay)

    async def _claim_next_with_retry(self, session: Session, jobs: JobRepository) -> Job | None:
        claim_args = self._claim_args()
        for attempt in range(_SQLITE_LOCK_RETRY_ATTEMPTS):
            try:
                return jobs.claim_next(**claim_args)
            except OperationalError as lock_error:
                if not _should_retry_sqlite_lock(lock_error, attempt):
                    raise
                record_metric("db_lock_retries")
                session.rollback()
                await self._sleep_before_retry(attempt)
        return None

    async def _succeed_with_retry(
        self,
        session: Session,
        jobs: JobRepository,
        job_id: int,
        job_kind: str,
        final_payload: dict[str, Any],
        *,
        enqueue_agent_after_discovery: bool = False,
        enqueue_notifications: bool = False,
    ) -> None:
        for attempt in range(_SQLITE_LOCK_RETRY_ATTEMPTS):
            try:
                job = session.get(Job, job_id)
                if job is None or job.status != JobStatus.RUNNING:
                    return
                job.payload = final_payload
                if enqueue_agent_after_discovery:
                    jobs.enqueue_agent_reviews()
                if enqueue_notifications:
                    jobs.enqueue_notification_delivery_if_needed()
                jobs.succeed(job)
                break
            except OperationalError as lock_error:
                if not _should_retry_sqlite_lock(lock_error, attempt):
                    raise
                record_metric("db_lock_retries")
                session.rollback()
                await self._sleep_before_retry(attempt)

        if job_kind != REFRESH_COVER_FINGERPRINTS:
            return
        if "has_more" in final_payload:
            should_continue = bool(final_payload["has_more"])
        else:
            should_continue = int(final_payload.get("remaining") or 0) > 0
        force = bool(final_payload.get("force", False))
        if not should_continue and not self.settings.agent_configured:
            return
        for attempt in range(_SQLITE_LOCK_RETRY_ATTEMPTS):
            try:
                if should_continue:
                    jobs.enqueue_cover_fingerprint_refresh(force=force)
                elif self.settings.agent_configured:
                    jobs.enqueue_agent_reviews()
                session.commit()
                return
            except OperationalError as lock_error:
                if not _should_retry_sqlite_lock(lock_error, attempt):
                    raise
                record_metric("db_lock_retries")
                session.rollback()
                await self._sleep_before_retry(attempt)

    async def _fail_with_retry(
        self, session: Session, jobs: JobRepository, job_id: int, error: str
    ) -> None:
        for attempt in range(_SQLITE_LOCK_RETRY_ATTEMPTS):
            try:
                job = session.get(Job, job_id)
                if job is None:
                    raise RuntimeError(f"任务 {job_id} 在失败处理期间被删除")
                if job.status != JobStatus.RUNNING:
                    return
                if (
                    job.kind == SOCIAL_PROCESS_POST
                    and job.attempts >= self.settings.max_job_attempts
                ):
                    state = session.get(SocialPostProcessing, int(job.payload["post_id"]))
                    if state:
                        state.status = "retry"
                        state.last_error = error[:1000]
                jobs.fail(job, error, self.settings.max_job_attempts)
                return
            except OperationalError as lock_error:
                if not _should_retry_sqlite_lock(lock_error, attempt):
                    raise
                record_metric("db_lock_retries")
                session.rollback()
                await self._sleep_before_retry(attempt)

    async def run_once(self) -> bool:
        with SessionLocal() as session:
            jobs = JobRepository(session)
            job = await self._claim_next_with_retry(session, jobs)
            if job is None:
                return False
            job_id = job.id
            job_kind = job.kind
            scheduled_at = job.next_attempt_at or job.created_at
            queue_wait_ms = (
                max(0, round((datetime.now(UTC) - as_utc(scheduled_at)).total_seconds() * 1000))
                if scheduled_at
                else 0
            )
            enqueue_agent_after_discovery = False
            enqueue_notifications = False
            with capture_metrics(f"job.{job_kind}", log_summary=True) as metrics:
                metrics.set_attr("job_id", job_id)
                metrics.set_attr("queue_wait_ms", queue_wait_ms)
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
                        final_payload = {**job.payload, **result}
                        enqueue_agent_after_discovery = (
                            self.settings.agent_review_after_discovery
                            and self.settings.agent_configured
                        )
                    elif job.kind == DOWNLOAD_CHAPTER:
                        path, verification = await DownloadService(
                            session, self.providers, self.settings
                        ).download(
                            int(job.payload["work_id"]),
                            str(job.payload["provider"]),
                            str(job.payload["chapter_id"]),
                        )
                        final_payload = {
                            **job.payload,
                            "output_path": path,
                            "verification": asdict(verification),
                        }
                    elif job.kind == REFRESH_COVER_FINGERPRINTS:
                        result = await CoverFingerprintRefreshService(session, self.providers).run(
                            bool(job.payload.get("force", False))
                        )
                        final_payload = {**job.payload, **result}
                    elif job.kind == AGENT_REVIEW_SUGGESTIONS:
                        result = await AgentReviewService(session, self.settings).run_pending(
                            job.payload.get("max_reviews")
                        )
                        final_payload = {**job.payload, **result}
                    elif job.kind == SOCIAL_SYNC_ACCOUNT:
                        result = await SocialSyncService(session, self.settings).sync_account(
                            int(job.payload["account_id"])
                        )
                        final_payload = {**job.payload, **result}
                        enqueue_notifications = True
                    elif job.kind == SOCIAL_PROCESS_POST:
                        result = await SocialSyncService(session, self.settings).process_post(
                            int(job.payload["post_id"])
                        )
                        final_payload = {**job.payload, **result}
                        enqueue_notifications = True
                    elif job.kind == REFRESH_SOCIAL_DIGEST:
                        digest = await DigestService(session, self.settings).refresh(
                            int(job.payload["author_id"])
                        )
                        session.commit()
                        final_payload = {**job.payload, "digest_id": digest.id if digest else None}
                    elif job.kind == BUILD_DAILY_DIGEST:
                        result = await DailyDigestService(session, self.settings).build(
                            force=bool(job.payload.get("force", False))
                        )
                        session.commit()
                        final_payload = {**job.payload, **result}
                        enqueue_notifications = True
                    elif job.kind == DELIVER_NOTIFICATIONS:
                        result = await NotificationService(session, self.settings).deliver_pending()
                        final_payload = {**job.payload, **result}
                    else:
                        raise ValueError(f"未知任务类型: {job.kind}")
                except Exception as exc:
                    logger.warning("Job %s failed: %s", job_id, exc)
                    session.rollback()
                    await self._fail_with_retry(session, jobs, job_id, str(exc))
                    return True

                await self._succeed_with_retry(
                    session,
                    jobs,
                    job_id,
                    job_kind,
                    final_payload,
                    enqueue_agent_after_discovery=enqueue_agent_after_discovery,
                    enqueue_notifications=enqueue_notifications,
                )
                record_metric("succeeded_count")
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


class GeneralWorkerThread(CoverWorkerThread):
    """Run discovery, social sync, digest, and review jobs off the API event loop."""

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.thread.name = "general-jobs"

    def _make_worker(self, providers: ProviderRegistry) -> JobWorker:
        return JobWorker(
            self.settings,
            providers,
            exclude_downloads=True,
            exclude_covers=True,
            exclude_notifications=True,
            exclude_social_processing=True,
        )


class DownloadWorkerThread(CoverWorkerThread):
    """Run chapter downloads and CBZ archiving off the API event loop."""

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.thread.name = "chapter-downloads"

    def _make_worker(self, providers: ProviderRegistry) -> "DownloadWorker":
        return DownloadWorker(self.settings, providers)


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

    async def _sleep_before_retry(self, attempt: int) -> None:
        delay = _sqlite_retry_delay(self.settings.poll_interval_seconds, attempt)
        await asyncio.sleep(delay)

    async def _claim_with_retry(self) -> tuple[int, dict] | None:
        for attempt in range(_SQLITE_LOCK_RETRY_ATTEMPTS):
            try:
                return await asyncio.to_thread(self._claim)
            except OperationalError as exc:
                if not _should_retry_sqlite_lock(exc, attempt):
                    raise
                await self._sleep_before_retry(attempt)
        return None

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
        for attempt in range(_SQLITE_LOCK_RETRY_ATTEMPTS):
            try:
                await asyncio.to_thread(self._finish, job_id, payload, error)
                return
            except OperationalError as exc:
                if not _should_retry_sqlite_lock(exc, attempt):
                    raise
                await self._sleep_before_retry(attempt)

    async def run_once(self) -> bool:
        claimed = await self._claim_with_retry()
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
