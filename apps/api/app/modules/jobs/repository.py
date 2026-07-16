from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db.models import DailyDigestDelivery, Job, JobStatus, NotificationOutbox

DISCOVER_AUTHOR = "discover_author"
DOWNLOAD_CHAPTER = "download_chapter"
AGENT_REVIEW_SUGGESTIONS = "agent_review_suggestions"
SOCIAL_SYNC_ACCOUNT = "social_sync_account"
DELIVER_NOTIFICATIONS = "deliver_notifications"
BUILD_DAILY_DIGEST = "build_daily_digest"
REFRESH_COVER_FINGERPRINTS = "refresh_cover_fingerprints"


class JobRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def enqueue_discovery(self, author_id: int) -> Job:
        active = self.session.scalars(
            select(Job).where(
                Job.kind == DISCOVER_AUTHOR,
                Job.status.in_([JobStatus.PENDING, JobStatus.RUNNING]),
            )
        )
        existing = next(
            (job for job in active if job.payload.get("author_id") == author_id), None
        )
        if existing:
            return existing
        job = Job(kind=DISCOVER_AUTHOR, payload={"author_id": author_id})
        self.session.add(job)
        self.session.flush()
        return job

    def enqueue_download(self, work_id: int, provider: str, chapter_id: str) -> Job:
        active = self.session.scalars(
            select(Job).where(
                Job.kind == DOWNLOAD_CHAPTER,
                Job.status.in_([JobStatus.PENDING, JobStatus.RUNNING]),
            )
        )
        payload = {"work_id": work_id, "provider": provider, "chapter_id": chapter_id}
        existing = next((job for job in active if job.payload == payload), None)
        if existing:
            return existing
        job = Job(kind=DOWNLOAD_CHAPTER, payload=payload)
        self.session.add(job)
        self.session.flush()
        return job

    def enqueue_agent_reviews(self, maximum: int | None = None) -> Job:
        active = self.session.scalar(
            select(Job).where(
                Job.kind == AGENT_REVIEW_SUGGESTIONS,
                Job.status.in_([JobStatus.PENDING, JobStatus.RUNNING]),
            )
        )
        if active:
            return active
        payload = {"max_reviews": maximum} if maximum is not None else {}
        job = Job(kind=AGENT_REVIEW_SUGGESTIONS, payload=payload)
        self.session.add(job)
        self.session.flush()
        return job

    def enqueue_cover_fingerprint_refresh(self, force: bool = False) -> Job:
        active = self.session.scalar(
            select(Job).where(
                Job.kind == REFRESH_COVER_FINGERPRINTS,
                Job.status.in_([JobStatus.PENDING, JobStatus.RUNNING]),
            )
        )
        if active:
            return active
        job = Job(kind=REFRESH_COVER_FINGERPRINTS, payload={"force": force})
        self.session.add(job)
        self.session.flush()
        return job

    def enqueue_social_sync(self, account_id: int) -> Job:
        active = self.session.scalars(
            select(Job).where(
                Job.kind == SOCIAL_SYNC_ACCOUNT,
                Job.status.in_([JobStatus.PENDING, JobStatus.RUNNING]),
            )
        )
        existing = next(
            (job for job in active if job.payload.get("account_id") == account_id), None
        )
        if existing:
            return existing
        job = Job(kind=SOCIAL_SYNC_ACCOUNT, payload={"account_id": account_id})
        self.session.add(job)
        self.session.flush()
        return job

    def enqueue_daily_digest(self, force: bool = False) -> Job:
        active = self.session.scalar(
            select(Job).where(
                Job.kind == BUILD_DAILY_DIGEST,
                Job.status.in_([JobStatus.PENDING, JobStatus.RUNNING]),
            )
        )
        if active:
            if force:
                active.payload = {**active.payload, "force": True}
            return active
        job = Job(kind=BUILD_DAILY_DIGEST, payload={"force": force})
        self.session.add(job)
        self.session.flush()
        return job

    def enqueue_notification_delivery_if_needed(self) -> Job | None:
        pending = self.session.scalar(
            select(NotificationOutbox.id).where(
                NotificationOutbox.status == "pending",
                or_(
                    NotificationOutbox.next_attempt_at.is_(None),
                    NotificationOutbox.next_attempt_at <= datetime.now(UTC),
                ),
            )
        )
        pending_digest = self.session.scalar(
            select(DailyDigestDelivery.id).where(
                DailyDigestDelivery.status == "pending",
                or_(
                    DailyDigestDelivery.next_attempt_at.is_(None),
                    DailyDigestDelivery.next_attempt_at <= datetime.now(UTC),
                ),
            )
        )
        if pending is None and pending_digest is None:
            return None
        active = self.session.scalar(
            select(Job).where(
                Job.kind == DELIVER_NOTIFICATIONS,
                Job.status.in_([JobStatus.PENDING, JobStatus.RUNNING]),
            )
        )
        if active:
            return active
        job = Job(kind=DELIVER_NOTIFICATIONS, payload={})
        self.session.add(job)
        self.session.flush()
        return job

    def claim_next(self) -> Job | None:
        job = self.session.scalar(
            select(Job)
            .where(Job.status == JobStatus.PENDING)
            .order_by(Job.created_at, Job.id)
            .limit(1)
        )
        if job:
            job.status = JobStatus.RUNNING
            job.attempts += 1
            job.started_at = datetime.now(UTC)
            job.error = None
            self.session.commit()
        return job

    def recover_interrupted(self) -> int:
        """Requeue jobs left RUNNING when the previous process stopped unexpectedly."""
        interrupted = list(
            self.session.scalars(select(Job).where(Job.status == JobStatus.RUNNING))
        )
        for job in interrupted:
            job.status = JobStatus.PENDING
            job.started_at = None
            job.error = "上次运行被服务重启中断，已自动重新排队"
        if interrupted:
            self.session.commit()
        return len(interrupted)

    def succeed(self, job: Job) -> None:
        job.status = JobStatus.SUCCEEDED
        job.finished_at = datetime.now(UTC)
        self.session.commit()

    def fail(self, job: Job, error: str, max_attempts: int) -> None:
        job.error = error[:4000]
        if job.attempts < max_attempts:
            job.status = JobStatus.PENDING
        else:
            job.status = JobStatus.FAILED
            job.finished_at = datetime.now(UTC)
        self.session.commit()

    def list_recent(self, limit: int = 20) -> list[Job]:
        return list(
            self.session.scalars(select(Job).order_by(Job.created_at.desc()).limit(limit))
        )
