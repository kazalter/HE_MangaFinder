from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class SeriesMemberInput(BaseModel):
    group_id: int = Field(ge=1)
    volume_number: int | None = Field(default=None, ge=1, le=999)
    label: str | None = Field(default=None, max_length=100)


class SeriesCreate(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    members: list[SeriesMemberInput] = Field(default_factory=list, max_length=500)
    candidate: bool = False


class SeriesMemberRead(BaseModel):
    group_id: int
    title: str
    volume_number: int | None
    label: str | None
    confidence: float
    is_manual: bool


class SeriesRead(BaseModel):
    id: int
    title: str
    members: list[SeriesMemberRead]
    missing_volumes: list[int]
    created_at: datetime
    updated_at: datetime


class SeriesCandidateGroup(BaseModel):
    group_id: int
    title: str
    volume_number: int


class SeriesCandidateRead(BaseModel):
    title: str
    normalized_title: str
    author_ids: list[int]
    groups: list[SeriesCandidateGroup]
    missing_volumes: list[int]


class WorkRelationCreate(BaseModel):
    source_group_id: int = Field(ge=1)
    target_group_id: int = Field(ge=1)
    relation_type: Literal["prequel", "sequel", "side_story", "compilation"]


class WorkRelationRead(BaseModel):
    id: int
    source_group_id: int
    source_title: str
    target_group_id: int
    target_title: str
    relation_type: str
    is_manual: bool
    created_at: datetime
