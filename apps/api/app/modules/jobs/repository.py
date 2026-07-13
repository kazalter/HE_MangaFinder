from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Job, JobStatus

DISCOVER_AUTHOR = "discover_author"
DOWNLOAD_CHAPTER = "download_chapter"
AGENT_REVIEW_SUGGESTIONS = "agent_review_suggestions"


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
