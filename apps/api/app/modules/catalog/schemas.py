from datetime import datetime

from pydantic import BaseModel, Field

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
    latest_source_at: datetime | None
    confidence: float
    match_method: str
    is_manual: bool
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
    latest_source_at: datetime | None
    edition_count: int
    providers: list[str]


class WorkGroupDetail(WorkGroupRead):
    editions: list[EditionRead]


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
