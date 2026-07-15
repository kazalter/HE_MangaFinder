from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class Author(Base):
    __tablename__ = "authors"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    works: Mapped[list["Work"]] = relationship(
        secondary="author_works", back_populates="authors", viewonly=True
    )


class Work(Base):
    __tablename__ = "works"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(500), index=True)
    description: Mapped[str | None] = mapped_column(Text)
    cover_url: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(String(50))
    year: Mapped[int | None]
    language: Mapped[str | None] = mapped_column(String(20))
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    authors: Mapped[list[Author]] = relationship(
        secondary="author_works", back_populates="works", viewonly=True
    )
    sources: Mapped[list["WorkSource"]] = relationship(
        back_populates="work", cascade="all, delete-orphan", lazy="selectin"
    )
    group_membership: Mapped["WorkGroupMember | None"] = relationship(
        back_populates="work", cascade="all, delete-orphan", lazy="selectin"
    )
    fingerprint: Mapped["WorkFingerprint | None"] = relationship(
        back_populates="work", cascade="all, delete-orphan", lazy="selectin"
    )


class WorkSource(Base):
    __tablename__ = "work_sources"
    __table_args__ = (UniqueConstraint("provider", "external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    work_id: Mapped[int] = mapped_column(ForeignKey("works.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(80), index=True)
    external_id: Mapped[str] = mapped_column(String(300))
    source_url: Mapped[str] = mapped_column(Text)
    raw_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    work: Mapped[Work] = relationship(back_populates="sources")


class AuthorWork(Base):
    __tablename__ = "author_works"

    author_id: Mapped[int] = mapped_column(
        ForeignKey("authors.id", ondelete="CASCADE"), primary_key=True
    )
    work_id: Mapped[int] = mapped_column(
        ForeignKey("works.id", ondelete="CASCADE"), primary_key=True
    )
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WorkGroup(Base):
    __tablename__ = "work_groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(500), index=True)
    description: Mapped[str | None] = mapped_column(Text)
    cover_url: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(String(50))
    year: Mapped[int | None]
    language: Mapped[str | None] = mapped_column(String(20))
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    latest_source_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    members: Mapped[list["WorkGroupMember"]] = relationship(
        back_populates="group", cascade="all, delete-orphan", lazy="selectin"
    )


class WorkGroupMember(Base):
    __tablename__ = "work_group_members"
    __table_args__ = (UniqueConstraint("work_id"),)

    group_id: Mapped[int] = mapped_column(
        ForeignKey("work_groups.id", ondelete="CASCADE"), primary_key=True
    )
    work_id: Mapped[int] = mapped_column(
        ForeignKey("works.id", ondelete="CASCADE"), primary_key=True
    )
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    match_method: Mapped[str] = mapped_column(String(50), default="new")
    is_manual: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    group: Mapped[WorkGroup] = relationship(back_populates="members")
    work: Mapped[Work] = relationship(back_populates="group_membership", lazy="selectin")


class WorkFingerprint(Base):
    __tablename__ = "work_fingerprints"

    work_id: Mapped[int] = mapped_column(
        ForeignKey("works.id", ondelete="CASCADE"), primary_key=True
    )
    normalized_title: Mapped[str] = mapped_column(String(500), index=True)
    title_aliases: Mapped[list[str]] = mapped_column(JSON, default=list)
    variant_labels: Mapped[list[str]] = mapped_column(JSON, default=list)
    page_count: Mapped[int | None]
    cover_hash: Mapped[str | None] = mapped_column(String(16), index=True)
    cover_fingerprint: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    work: Mapped[Work] = relationship(back_populates="fingerprint")


class MergeSuggestion(Base):
    __tablename__ = "merge_suggestions"
    __table_args__ = (UniqueConstraint("source_group_id", "target_group_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source_group_id: Mapped[int] = mapped_column(
        ForeignKey("work_groups.id", ondelete="CASCADE"), index=True
    )
    target_group_id: Mapped[int] = mapped_column(
        ForeignKey("work_groups.id", ondelete="CASCADE"), index=True
    )
    confidence: Mapped[float] = mapped_column(Float)
    reasons: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AgentReview(Base):
    """Immutable audit record for one model review of a merge suggestion."""

    __tablename__ = "agent_reviews"
    id: Mapped[int] = mapped_column(primary_key=True)
    suggestion_id: Mapped[int | None] = mapped_column(
        ForeignKey("merge_suggestions.id", ondelete="SET NULL"), index=True
    )
    candidate_key: Mapped[str] = mapped_column(String(64), index=True)
    evidence_hash: Mapped[str] = mapped_column(String(64), index=True)
    provider: Mapped[str] = mapped_column(String(50))
    model: Mapped[str] = mapped_column(String(200))
    prompt_version: Mapped[str] = mapped_column(String(50))
    schema_version: Mapped[str] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(20), index=True)
    decision: Mapped[str | None] = mapped_column(String(30), index=True)
    confidence: Mapped[float | None] = mapped_column(Float)
    relation: Mapped[str | None] = mapped_column(String(40))
    canonical_title: Mapped[str | None] = mapped_column(String(500))
    evidence_codes: Mapped[list[str]] = mapped_column(JSON, default=list)
    conflict_codes: Mapped[list[str]] = mapped_column(JSON, default=list)
    rationale: Mapped[str | None] = mapped_column(Text)
    input_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    raw_output: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PairConstraint(Base):
    """Durable human/model-assisted negative relation between two source identities."""

    __tablename__ = "pair_constraints"

    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    decision: Mapped[str] = mapped_column(String(30), default="different_work")
    origin: Mapped[str] = mapped_column(String(30), default="human_rejection")
    review_id: Mapped[int | None] = mapped_column(
        ForeignKey("agent_reviews.id", ondelete="SET NULL")
    )
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(80), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus), default=JobStatus.PENDING, index=True
    )
    attempts: Mapped[int] = mapped_column(default=0)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SocialAccount(Base):
    __tablename__ = "social_accounts"
    __table_args__ = (UniqueConstraint("author_id", "platform", "handle"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    author_id: Mapped[int] = mapped_column(
        ForeignKey("authors.id", ondelete="CASCADE"), index=True
    )
    platform: Mapped[str] = mapped_column(String(30), default="x", index=True)
    handle: Mapped[str] = mapped_column(String(100), index=True)
    platform_user_id: Mapped[str | None] = mapped_column(String(100), index=True)
    display_name: Mapped[str | None] = mapped_column(String(200))
    profile_url: Mapped[str | None] = mapped_column(Text)
    avatar_url: Mapped[str | None] = mapped_column(Text)
    account_type: Mapped[str] = mapped_column(String(30), default="personal")
    status: Mapped[str] = mapped_column(String(30), default="suggested", index=True)
    match_score: Mapped[float | None] = mapped_column(Float)
    evidence: Mapped[list[str]] = mapped_column(JSON, default=list)
    last_post_id: Mapped[str | None] = mapped_column(String(100))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    sync_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class SocialPost(Base):
    __tablename__ = "social_posts"
    __table_args__ = (UniqueConstraint("account_id", "platform_post_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("social_accounts.id", ondelete="CASCADE"), index=True
    )
    platform_post_id: Mapped[str] = mapped_column(String(100), index=True)
    post_type: Mapped[str] = mapped_column(String(30), default="original", index=True)
    text: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str] = mapped_column(Text)
    conversation_id: Mapped[str | None] = mapped_column(String(100), index=True)
    replied_to_post_id: Mapped[str | None] = mapped_column(String(100))
    quoted_post_id: Mapped[str | None] = mapped_column(String(100))
    media: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    links: Mapped[list[str]] = mapped_column(JSON, default=list)
    ocr_text: Mapped[str | None] = mapped_column(Text)
    raw_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    posted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class EventRegistry(Base):
    __tablename__ = "events"

    code: Mapped[str] = mapped_column(String(30), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    venue: Mapped[str | None] = mapped_column(String(300))
    timezone: Mapped[str] = mapped_column(String(50), default="Asia/Tokyo")
    source_url: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ReleaseSignal(Base):
    __tablename__ = "release_signals"

    id: Mapped[int] = mapped_column(primary_key=True)
    author_id: Mapped[int] = mapped_column(
        ForeignKey("authors.id", ondelete="CASCADE"), index=True
    )
    primary_post_id: Mapped[int] = mapped_column(
        ForeignKey("social_posts.id", ondelete="CASCADE"), unique=True, index=True
    )
    cluster_key: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(40), index=True)
    title: Mapped[str | None] = mapped_column(String(500), index=True)
    event_code: Mapped[str | None] = mapped_column(String(30), index=True)
    booth: Mapped[str | None] = mapped_column(String(100))
    release_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    store_urls: Mapped[list[str]] = mapped_column(JSON, default=list)
    confidence: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    evidence: Mapped[list[str]] = mapped_column(JSON, default=list)
    counter_evidence: Mapped[list[str]] = mapped_column(JSON, default=list)
    missing_information: Mapped[list[str]] = mapped_column(JSON, default=list)
    linked_group_id: Mapped[int | None] = mapped_column(
        ForeignKey("work_groups.id", ondelete="SET NULL"), index=True
    )
    reviewed_by: Mapped[str | None] = mapped_column(String(30))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ReleaseSignalPost(Base):
    __tablename__ = "release_signal_posts"

    signal_id: Mapped[int] = mapped_column(
        ForeignKey("release_signals.id", ondelete="CASCADE"), primary_key=True
    )
    post_id: Mapped[int] = mapped_column(
        ForeignKey("social_posts.id", ondelete="CASCADE"), primary_key=True, unique=True
    )
    relation: Mapped[str] = mapped_column(String(30), default="evidence")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SocialAgentReview(Base):
    __tablename__ = "social_agent_reviews"

    id: Mapped[int] = mapped_column(primary_key=True)
    signal_id: Mapped[int | None] = mapped_column(
        ForeignKey("release_signals.id", ondelete="SET NULL"), index=True
    )
    post_id: Mapped[int] = mapped_column(
        ForeignKey("social_posts.id", ondelete="CASCADE"), index=True
    )
    evidence_hash: Mapped[str] = mapped_column(String(64), index=True)
    model: Mapped[str] = mapped_column(String(200))
    prompt_version: Mapped[str] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(30), index=True)
    verdict: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    input_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class NotificationOutbox(Base):
    __tablename__ = "notification_outbox"

    id: Mapped[int] = mapped_column(primary_key=True)
    signal_id: Mapped[int] = mapped_column(
        ForeignKey("release_signals.id", ondelete="CASCADE"), index=True
    )
    channel: Mapped[str] = mapped_column(String(30), index=True)
    event: Mapped[str] = mapped_column(String(30), default="signal_created")
    idempotency_key: Mapped[str] = mapped_column(String(160), unique=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    error: Mapped[str | None] = mapped_column(Text)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
