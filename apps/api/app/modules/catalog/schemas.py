from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.modules.agent_review.schemas import AgentReviewRead


class WorkSourceRead(BaseModel):
    provider: str
    external_id: str
    source_url: str
    source_updated_at: datetime | None = None


class WorkRead(BaseModel):
    id: int
    title: str
    description: str | None
    cover_url: str | None
    status: str | None
    year: int | None
    language: str | None
    tags: list[str]
    discovered_at: datetime
    sources: list[WorkSourceRead]


class ChapterRead(BaseModel):
    external_id: str
    title: str | None
    number: str | None
    language: str
    published_at: datetime | None
    source_url: str


class DownloadCreate(BaseModel):
    provider: str
    chapter_id: str = Field(min_length=1, max_length=300)


class WorkGroupAuthorRead(BaseModel):
    id: int
    name: str


class EditionRead(BaseModel):
    work_id: int
    title: str
    description: str | None
    cover_url: str | None
    status: str | None
    year: int | None
    language: str | None
    tags: list[str]
    variant_labels: list[str]
    page_count: int | None = None
    latest_source_at: datetime | None
    first_discovered_at: datetime | None = None
    confidence: float
    match_method: str
    is_manual: bool
    authors: list[WorkGroupAuthorRead]
    sources: list[WorkSourceRead]


class WorkGroupRead(BaseModel):
    id: int
    title: str
    description: str | None
    cover_url: str | None
    status: str | None
    year: int | None
    language: str | None
    tags: list[str]
    first_source_at: datetime | None
    latest_source_at: datetime | None
    edition_count: int
    providers: list[str]
    authors: list[WorkGroupAuthorRead]


class WorkSeriesRead(BaseModel):
    id: int
    title: str
    volume_number: int | None
    label: str | None
    missing_volumes: list[int] = Field(default_factory=list)


class WorkRelationSummary(BaseModel):
    id: int
    related_group_id: int
    related_title: str
    relation_type: str
    direction: str


class MetadataSourceRead(BaseModel):
    locked: bool
    edition_ids: list[int] = Field(default_factory=list)
    providers: list[str] = Field(default_factory=list)


class MetadataUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=500)
    description: str | None = Field(default=None, max_length=50000)
    year: int | None = Field(default=None, ge=1, le=3000, strict=True)
    status: Literal["ongoing", "completed", "hiatus", "cancelled"] | None = None
    language: str | None = Field(default=None, min_length=1, max_length=20)
    tags: list[str] | None = Field(default=None, max_length=100)
    cover_work_id: int | None = Field(default=None, gt=0, strict=True)
    preferred_work_id: int | None = Field(default=None, gt=0, strict=True)


class WorkGroupDetail(WorkGroupRead):
    first_discovered_at: datetime | None = None
    metadata_overrides: dict[str, Any] = Field(default_factory=dict)
    metadata_sources: dict[str, MetadataSourceRead] = Field(default_factory=dict)
    editions: list[EditionRead]
    series: WorkSeriesRead | None = None
    relations: list[WorkRelationSummary] = Field(default_factory=list)


class GroupMergeCreate(BaseModel):
    source_group_id: int


class MergeSuggestionRead(BaseModel):
    id: int
    source_group_id: int
    source_title: str
    target_group_id: int
    target_title: str
    source_group: WorkGroupRead | None = None
    target_group: WorkGroupRead | None = None
    confidence: float
    reasons: list[str]
    status: str
    agent_review: AgentReviewRead | None = None
    hard_conflicts: list[str] = Field(default_factory=list)
    soft_conflicts: list[str] = Field(default_factory=list)
    conflict_details: list[str] = Field(default_factory=list)
    core_title_similarity: float | None = None
    cover_hash_distance: int | None = None
    cover_match_mode: str | None = None
    cover_legacy_distance: int | None = None
    source_identity_titles: list[str] = Field(default_factory=list)
    target_identity_titles: list[str] = Field(default_factory=list)
    shared_context: list[str] = Field(default_factory=list)
