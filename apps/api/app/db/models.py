from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Enum,
    Float,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.types import UTCDateTime


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
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list)
    circle_names: Mapped[list[str]] = mapped_column(JSON, default=list)
    provider_query_names: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    identity_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    last_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    works: Mapped[list["Work"]] = relationship(
        secondary="author_works", back_populates="authors", viewonly=True
    )


class AuthorIdentitySuggestion(Base):
    __tablename__ = "author_identity_suggestions"
    __table_args__ = (UniqueConstraint("author_id", "provider", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("authors.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(80))
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


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
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow, onupdate=utcnow
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
    source_updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    work: Mapped[Work] = relationship(back_populates="sources")


class AuthorWork(Base):
    __tablename__ = "author_works"

    author_id: Mapped[int] = mapped_column(
        ForeignKey("authors.id", ondelete="CASCADE"), primary_key=True
    )
    work_id: Mapped[int] = mapped_column(
        ForeignKey("works.id", ondelete="CASCADE"), primary_key=True
    )
    discovered_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class AuthorWorkExclusion(Base):
    """A user rejection that prevents one source result from being rediscovered."""

    __tablename__ = "author_work_exclusions"
    __table_args__ = (UniqueConstraint("author_id", "provider", "external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    author_id: Mapped[int] = mapped_column(
        ForeignKey("authors.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(80), index=True)
    external_id: Mapped[str] = mapped_column(String(300))
    title: Mapped[str] = mapped_column(String(500))
    source_url: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(String(80), default="human_rejection")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


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
    manual_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    first_source_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    latest_source_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow, onupdate=utcnow
    )

    members: Mapped[list["WorkGroupMember"]] = relationship(
        back_populates="group", cascade="all, delete-orphan", lazy="selectin"
    )


class Series(Base):
    __tablename__ = "series"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(500), index=True)
    normalized_title: Mapped[str] = mapped_column(String(500), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow, onupdate=utcnow
    )


class SeriesMember(Base):
    __tablename__ = "series_members"
    __table_args__ = (UniqueConstraint("group_id"),)

    series_id: Mapped[int] = mapped_column(
        ForeignKey("series.id", ondelete="CASCADE"), primary_key=True
    )
    group_id: Mapped[int] = mapped_column(
        ForeignKey("work_groups.id", ondelete="CASCADE"), primary_key=True
    )
    volume_number: Mapped[int | None] = mapped_column(index=True)
    label: Mapped[str | None] = mapped_column(String(100))
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    is_manual: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class WorkRelation(Base):
    __tablename__ = "work_relations"
    __table_args__ = (
        UniqueConstraint("source_group_id", "target_group_id", "relation_type"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_group_id: Mapped[int] = mapped_column(
        ForeignKey("work_groups.id", ondelete="CASCADE"), index=True
    )
    target_group_id: Mapped[int] = mapped_column(
        ForeignKey("work_groups.id", ondelete="CASCADE"), index=True
    )
    relation_type: Mapped[str] = mapped_column(String(30), index=True)
    is_manual: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


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
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    group: Mapped[WorkGroup] = relationship(back_populates="members")
    work: Mapped[Work] = relationship(back_populates="group_membership", lazy="selectin")


class CatalogOperation(Base):
    """Reversible user catalog operation; change records are private internal data."""

    __tablename__ = "catalog_operations"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(40), index=True)
    group_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    before_group_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    after_group_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    summary: Mapped[str] = mapped_column(Text)
    changes: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    reference_state: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    undone_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


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
        UTCDateTime(), default=utcnow, onupdate=utcnow
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
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


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
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


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
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow, onupdate=utcnow
    )


class DiscoveryRun(Base):
    """One auditable catalog scan for an author across all enabled providers."""

    __tablename__ = "discovery_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    author_id: Mapped[int] = mapped_column(
        ForeignKey("authors.id", ondelete="CASCADE"), index=True
    )
    trigger: Mapped[str] = mapped_column(String(30), default="manual", index=True)
    status: Mapped[str] = mapped_column(String(30), default="running", index=True)
    found_count: Mapped[int] = mapped_column(default=0)
    new_count: Mapped[int] = mapped_column(default=0)
    updated_count: Mapped[int] = mapped_column(default=0)
    excluded_count: Mapped[int] = mapped_column(default=0)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)


class ProviderScanRun(Base):
    """Provider-level result retained even when another provider makes the job succeed."""

    __tablename__ = "provider_scan_runs"
    __table_args__ = (UniqueConstraint("discovery_run_id", "provider"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    discovery_run_id: Mapped[int] = mapped_column(
        ForeignKey("discovery_runs.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(80), index=True)
    query_name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(30), default="running", index=True)
    found_count: Mapped[int] = mapped_column(default=0)
    new_count: Mapped[int] = mapped_column(default=0)
    updated_count: Mapped[int] = mapped_column(default=0)
    excluded_count: Mapped[int] = mapped_column(default=0)
    duration_ms: Mapped[int] = mapped_column(default=0)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class CatalogEvent(Base):
    """Durable discovery inbox item and the source of catalog growth statistics."""

    __tablename__ = "catalog_events"
    __table_args__ = (
        UniqueConstraint("author_id", "provider", "external_id", "event_type"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    author_id: Mapped[int] = mapped_column(
        ForeignKey("authors.id", ondelete="CASCADE"), index=True
    )
    group_id: Mapped[int | None] = mapped_column(
        ForeignKey("work_groups.id", ondelete="SET NULL"), index=True
    )
    work_id: Mapped[int | None] = mapped_column(
        ForeignKey("works.id", ondelete="SET NULL"), index=True
    )
    provider: Mapped[str] = mapped_column(String(80), index=True)
    external_id: Mapped[str] = mapped_column(String(300))
    event_type: Mapped[str] = mapped_column(String(40), index=True)
    title: Mapped[str] = mapped_column(String(500))
    state: Mapped[str] = mapped_column(String(30), default="unread", index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)
    read_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


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
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)


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
    last_synced_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    next_sync_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    sync_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow, onupdate=utcnow
    )


class SocialSyncProgress(Base):
    """Durable continuation for a timeline scan; the public watermark stays unchanged."""

    __tablename__ = "social_sync_progress"

    account_id: Mapped[int] = mapped_column(
        ForeignKey("social_accounts.id", ondelete="CASCADE"), primary_key=True
    )
    handle: Mapped[str] = mapped_column(String(100))
    since_id: Mapped[str | None] = mapped_column(String(100))
    cursor: Mapped[str] = mapped_column(Text)
    newest_post_id: Mapped[str | None] = mapped_column(String(100))
    newest_posted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    backfill_cutoff: Mapped[datetime | None] = mapped_column(UTCDateTime())


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
    posted_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    edited_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    availability_status: Mapped[str] = mapped_column(
        String(30), default="available", index=True
    )
    availability_reason: Mapped[str | None] = mapped_column(Text)
    last_availability_checked_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(), index=True
    )
    unavailable_since: Mapped[datetime | None] = mapped_column(UTCDateTime())
    availability_failure_count: Mapped[int] = mapped_column(default=0)
    ingested_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow, onupdate=utcnow
    )


class SocialPostProcessing(Base):
    """Durable processing/OCR state, separate from the original post."""

    __tablename__ = "social_post_processing"

    post_id: Mapped[int] = mapped_column(
        ForeignKey("social_posts.id", ondelete="CASCADE"), primary_key=True
    )
    content_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    ocr_status: Mapped[str] = mapped_column(String(30), default="pending")
    backfill_only: Mapped[bool] = mapped_column(Boolean, default=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class SocialMediaAsset(Base):
    """Bounded local image archive; the source post remains the durable fact record."""

    __tablename__ = "social_media_assets"
    __table_args__ = (UniqueConstraint("post_id", "media_index"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(
        ForeignKey("social_posts.id", ondelete="CASCADE"), index=True
    )
    media_index: Mapped[int] = mapped_column()
    source_url: Mapped[str] = mapped_column(Text)
    local_path: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    mime_type: Mapped[str | None] = mapped_column(String(100))
    byte_size: Mapped[int] = mapped_column(default=0)
    width: Mapped[int | None] = mapped_column()
    height: Mapped[int | None] = mapped_column()
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    importance: Mapped[str] = mapped_column(String(20), default="normal", index=True)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    last_accessed_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow, index=True
    )
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow, onupdate=utcnow
    )


class ActivityItem(Base):
    """A grounded author activity topic assembled from one or more social posts."""

    __tablename__ = "activity_items"
    __table_args__ = (UniqueConstraint("author_id", "cluster_key"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    author_id: Mapped[int] = mapped_column(
        ForeignKey("authors.id", ondelete="CASCADE"), index=True
    )
    primary_post_id: Mapped[int] = mapped_column(
        ForeignKey("social_posts.id", ondelete="CASCADE"), index=True
    )
    cluster_key: Mapped[str] = mapped_column(String(64), index=True)
    category: Mapped[str] = mapped_column(String(40), index=True)
    headline: Mapped[str] = mapped_column(String(500))
    summary: Mapped[str] = mapped_column(Text, default="")
    importance: Mapped[str] = mapped_column(String(20), default="normal", index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    ended_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow, onupdate=utcnow
    )


class ActivityItemPost(Base):
    __tablename__ = "activity_item_posts"

    activity_id: Mapped[int] = mapped_column(
        ForeignKey("activity_items.id", ondelete="CASCADE"), primary_key=True
    )
    post_id: Mapped[int] = mapped_column(
        ForeignKey("social_posts.id", ondelete="CASCADE"), primary_key=True, unique=True
    )
    relation: Mapped[str] = mapped_column(String(30), default="evidence")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class AuthorDigest(Base):
    __tablename__ = "author_digests"
    __table_args__ = (UniqueConstraint("author_id", "period_type", "period_end"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    author_id: Mapped[int] = mapped_column(
        ForeignKey("authors.id", ondelete="CASCADE"), index=True
    )
    period_type: Mapped[str] = mapped_column(String(30), default="rolling_7d", index=True)
    period_start: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    period_end: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    highlights: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    uncertainties: Mapped[list[str]] = mapped_column(JSON, default=list)
    evidence_post_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    generated_by: Mapped[str] = mapped_column(String(30), default="rules")
    model: Mapped[str | None] = mapped_column(String(200))
    prompt_version: Mapped[str | None] = mapped_column(String(50))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow, onupdate=utcnow
    )


class EventRegistry(Base):
    __tablename__ = "events"

    code: Mapped[str] = mapped_column(String(30), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list)
    starts_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    ends_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    venue: Mapped[str | None] = mapped_column(String(300))
    timezone: Mapped[str] = mapped_column(String(50), default="Asia/Tokyo")
    source_url: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow, onupdate=utcnow
    )


class PlannedRelease(Base):
    """A work lifecycle shared by multiple social signals before catalog discovery."""

    __tablename__ = "planned_releases"
    __table_args__ = (UniqueConstraint("author_id", "normalized_title"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    author_id: Mapped[int] = mapped_column(
        ForeignKey("authors.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(500), index=True)
    normalized_title: Mapped[str] = mapped_column(String(500), index=True)
    status: Mapped[str] = mapped_column(String(30), default="announced", index=True)
    release_date: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    event_code: Mapped[str | None] = mapped_column(String(30), index=True)
    booth: Mapped[str | None] = mapped_column(String(100))
    linked_group_id: Mapped[int | None] = mapped_column(
        ForeignKey("work_groups.id", ondelete="SET NULL"), index=True
    )
    suggested_group_id: Mapped[int | None] = mapped_column(
        ForeignKey("work_groups.id", ondelete="SET NULL"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow, onupdate=utcnow
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
    release_date: Mapped[datetime | None] = mapped_column(UTCDateTime())
    store_urls: Mapped[list[str]] = mapped_column(JSON, default=list)
    confidence: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    evidence: Mapped[list[str]] = mapped_column(JSON, default=list)
    counter_evidence: Mapped[list[str]] = mapped_column(JSON, default=list)
    missing_information: Mapped[list[str]] = mapped_column(JSON, default=list)
    planned_release_id: Mapped[int | None] = mapped_column(
        ForeignKey("planned_releases.id", ondelete="SET NULL"), index=True
    )
    linked_group_id: Mapped[int | None] = mapped_column(
        ForeignKey("work_groups.id", ondelete="SET NULL"), index=True
    )
    reviewed_by: Mapped[str | None] = mapped_column(String(30))
    reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    notified_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow, onupdate=utcnow
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
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


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
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


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
    next_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), index=True)
    error: Mapped[str | None] = mapped_column(Text)
    delivered_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class DailyDigestDelivery(Base):
    """One auditable, idempotent aggregate author-activity delivery per local date."""

    __tablename__ = "daily_digest_deliveries"

    id: Mapped[int] = mapped_column(primary_key=True)
    local_date: Mapped[str] = mapped_column(String(10), unique=True, index=True)
    timezone: Mapped[str] = mapped_column(String(50))
    period_start: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    period_end: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    author_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        UTCDateTime(), index=True
    )
    error: Mapped[str | None] = mapped_column(Text)
    delivered_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class SystemSetting(Base):
    """One persisted runtime configuration override."""

    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    encrypted: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow, onupdate=utcnow
    )


class AdminAccount(Base):
    __tablename__ = "admin_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True, default="admin")
    password_hash: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), default=utcnow, onupdate=utcnow
    )


class AdminSession(Base):
    __tablename__ = "admin_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("admin_accounts.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class ConfigAuditEvent(Base):
    __tablename__ = "config_audit_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("admin_accounts.id", ondelete="SET NULL"), index=True
    )
    action: Mapped[str] = mapped_column(String(100), index=True)
    changed_keys: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
