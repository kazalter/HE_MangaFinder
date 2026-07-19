import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

SignalKind = Literal[
    "new_release",
    "release_preview",
    "cover_reveal",
    "event_participation",
    "preorder",
    "on_sale",
    "reprint",
    "delay",
    "cancellation",
    "other",
]


class SocialAccountCreate(BaseModel):
    handle: str = Field(min_length=1, max_length=100)
    account_type: Literal["personal", "circle"] = "personal"
    confirmed: bool = False
    display_name: str | None = Field(default=None, max_length=200)

    @field_validator("handle")
    @classmethod
    def normalize_handle(cls, value: str) -> str:
        handle = value.strip().removeprefix("@").strip("/")
        if not re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle):
            raise ValueError("X 账号格式不正确")
        return handle.casefold()


class SocialAccountRead(BaseModel):
    id: int
    author_id: int
    platform: str
    handle: str
    display_name: str | None
    profile_url: str | None
    avatar_url: str | None
    account_type: str
    status: str
    match_score: float | None
    evidence: list[str]
    last_synced_at: datetime | None
    next_sync_at: datetime | None
    sync_error: str | None

    model_config = ConfigDict(from_attributes=True)


class SocialAccountSuggestion(BaseModel):
    handle: str
    display_name: str | None = None
    profile_url: str
    avatar_url: str | None = None
    score: float = Field(ge=0, le=1)
    evidence: list[str] = Field(default_factory=list)


class SocialPostRead(BaseModel):
    id: int
    platform_post_id: str
    post_type: str
    text: str
    url: str
    media: list[dict[str, object]]
    links: list[str]
    ocr_text: str | None
    posted_at: datetime
    availability_status: str
    availability_reason: str | None
    last_availability_checked_at: datetime | None

    model_config = ConfigDict(from_attributes=True)


ActivityCategory = Literal[
    "creation_progress",
    "release",
    "event",
    "sales",
    "artwork",
    "collaboration",
    "schedule_notice",
    "personal",
    "other",
]


class ActivityItemRead(BaseModel):
    id: int
    author_id: int
    author_name: str
    category: str
    headline: str
    summary: str
    importance: str
    confidence: float
    is_read: bool
    started_at: datetime
    ended_at: datetime
    posts: list[SocialPostRead]


class DigestHighlight(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    category: ActivityCategory
    importance: Literal["critical", "high", "normal", "low"] = "normal"
    factuality: Literal["fact", "plan", "inference"] = "fact"
    post_ids: list[int] = Field(min_length=1, max_length=10)


class ActivityDigestVerdict(BaseModel):
    summary: str = Field(min_length=1, max_length=1200)
    highlights: list[DigestHighlight] = Field(default_factory=list, max_length=12)
    uncertainties: list[str] = Field(default_factory=list, max_length=10)


class AuthorDigestRead(BaseModel):
    id: int
    author_id: int
    author_name: str
    period_type: str
    period_start: datetime
    period_end: datetime
    summary: str
    highlights: list[DigestHighlight]
    uncertainties: list[str]
    evidence_post_ids: list[int]
    generated_by: str
    model: str | None
    error: str | None
    created_at: datetime
    updated_at: datetime


class ReleaseSignalRead(BaseModel):
    id: int
    author_id: int
    author_name: str
    kind: str
    title: str | None
    event_code: str | None
    booth: str | None
    release_date: datetime | None
    store_urls: list[str]
    confidence: float
    status: str
    is_read: bool
    evidence: list[str]
    counter_evidence: list[str]
    missing_information: list[str]
    linked_group_id: int | None
    reviewed_by: str | None
    created_at: datetime
    updated_at: datetime
    posts: list[SocialPostRead]


class SignalReviewRequest(BaseModel):
    decision: Literal["confirm", "reject"]


class SignalLinkRequest(BaseModel):
    group_id: int


class SocialStatusRead(BaseModel):
    enabled: bool
    collector_configured: bool
    agent_configured: bool
    qq_configured: bool
    auto_confirm_threshold: float
    candidate_threshold: float
    pending_count: int
    unread_count: int
    daily_digest_enabled: bool
    daily_digest_hour: int
    daily_digest_timezone: str


class SocialAgentVerdict(BaseModel):
    kind: SignalKind
    is_authors_work: bool | None
    has_new_work: bool
    title: str | None = Field(default=None, max_length=500)
    event_code: str | None = Field(default=None, max_length=30)
    booth: str | None = Field(default=None, max_length=100)
    store_urls: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    evidence: list[str] = Field(default_factory=list, max_length=10)
    counter_evidence: list[str] = Field(default_factory=list, max_length=10)
    missing_information: list[str] = Field(default_factory=list, max_length=10)
    rationale: str = Field(max_length=1200)
