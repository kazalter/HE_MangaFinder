from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "v3"

EvidenceCode = Literal[
    "title_similarity",
    "normalized_title_match",
    "number_match",
    "cover_hash_match",
    "page_count_match",
    "year_match",
    "author_match",
    "provider_overlap",
    "variant_difference",
    "language_difference",
    "core_title_match",
    "core_title_similarity",
    "source_alias_match",
    "cover_hash_strong",
    "cover_hash_weak",
    "cover_crop_match",
]
ConflictCode = Literal[
    "number_mismatch",
    "author_mismatch",
    "page_count_mismatch",
    "year_mismatch",
    "insufficient_evidence",
    "core_title_mismatch",
    "cover_dissimilar",
]
Decision = Literal["same_work", "different_work", "uncertain"]
Relation = Literal[
    "same_edition",
    "translation",
    "colored",
    "uncensored",
    "remaster",
    "mixed_variants",
    "unrelated",
    "unknown",
]


class SourceEvidence(BaseModel):
    provider: str
    external_id: str
    source_updated_at: str | None = None
    metadata: dict[str, object] = Field(default_factory=dict)


class EditionEvidence(BaseModel):
    work_id: int
    raw_title: str
    normalized_title: str
    identity_core: str
    context_terms: list[str]
    aliases: list[str]
    number_signature: list[int]
    variant_labels: list[str]
    language: str | None
    year: int | None
    page_count: int | None
    cover_hash: str | None
    cover_fingerprint_version: int | None = None
    cover_width: int | None = None
    cover_height: int | None = None
    tags: list[str]
    authors: list[str]
    sources: list[SourceEvidence]


class GroupEvidence(BaseModel):
    group_id: int
    display_title: str
    has_manual_members: bool
    editions: list[EditionEvidence]


class CandidateEvidence(BaseModel):
    candidate_key: str
    suggestion_id: int
    rule_confidence: float
    rule_reasons: list[str]
    title_similarity: float
    core_title_similarity: float
    cover_hash_distance: int | None
    cover_hash_distances: list[int] = Field(default_factory=list)
    cover_match_mode: Literal["full", "crop", "legacy"] | None = None
    cover_legacy_distance: int | None = None
    cover_negative_reliable: bool = False
    page_count_delta: int | None = None
    page_count_ratio: float | None = None
    shared_context: list[str] = Field(default_factory=list)
    context_only: list[str] = Field(default_factory=list)
    available_evidence: list[EvidenceCode]
    hard_conflicts: list[ConflictCode]
    soft_conflicts: list[ConflictCode] = Field(default_factory=list)
    left: GroupEvidence
    right: GroupEvidence


class AgentVerdict(BaseModel):
    decision: Decision
    confidence: float = Field(ge=0, le=1)
    canonical_title: str = Field(max_length=500)
    relation: Relation
    evidence: list[EvidenceCode] = Field(max_length=10)
    conflicts: list[ConflictCode] = Field(max_length=5)
    rationale: str = Field(
        min_length=1,
        max_length=800,
        description="使用简体中文说明判断依据；作品原名、证据代码和数字可以保留原文。",
    )
    recommended_action: Literal["suggest_merge", "keep_separate", "human_review"]
    identity_title_left: str = Field(default="", max_length=500)
    identity_title_right: str = Field(default="", max_length=500)
    shared_context: list[str] = Field(default_factory=list, max_length=10)
    decisive_differences: list[str] = Field(
        default_factory=list,
        max_length=10,
        description="使用简体中文列出关键差异；作品原名和数字可以保留原文。",
    )

    model_config = ConfigDict(extra="forbid")


class AgentReviewRead(BaseModel):
    id: int
    suggestion_id: int | None
    status: str
    decision: Decision | None
    confidence: float | None
    relation: Relation | None
    canonical_title: str | None
    evidence_codes: list[str]
    conflict_codes: list[str]
    rationale: str | None
    model: str
    prompt_version: str
    error: str | None
    created_at: datetime
    is_stale: bool = False


class AgentRunCreate(BaseModel):
    max_reviews: int | None = Field(default=None, ge=1, le=100)


class AgentStatusRead(BaseModel):
    enabled: bool
    configured: bool
    provider: str
    model: str
    prompt_version: str
    auto_apply: bool
    sends_images: bool
    review_after_discovery: bool
