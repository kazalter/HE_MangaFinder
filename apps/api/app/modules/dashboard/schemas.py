from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class DashboardSummary(BaseModel):
    authors: int
    works: int
    editions: int
    new_works: int
    new_editions: int
    unread_discoveries: int
    pending_merges: int
    pending_signals: int
    failed_provider_scans: int
    incomplete_works: int


class TrendPoint(BaseModel):
    date: str
    works: int = 0
    editions: int = 0


class SourceHealth(BaseModel):
    provider: str
    scans: int
    succeeded: int
    failed: int
    success_rate: float
    new_items: int
    average_duration_ms: int
    last_success_at: datetime | None = None
    last_error: str | None = None


class UpcomingRelease(BaseModel):
    id: int
    author_id: int
    author_name: str
    title: str | None
    kind: str
    status: str
    release_date: datetime
    event_code: str | None
    booth: str | None
    linked_group_id: int | None


class DashboardRead(BaseModel):
    period_days: int
    summary: DashboardSummary
    trend: list[TrendPoint]
    source_health: list[SourceHealth]
    upcoming: list[UpcomingRelease]


class CatalogEventRead(BaseModel):
    id: int
    author_id: int
    author_name: str
    group_id: int | None
    work_id: int | None
    provider: str
    external_id: str
    event_type: str
    title: str
    cover_url: str | None
    state: str
    created_at: datetime
    read_at: datetime | None


class CatalogEventStateUpdate(BaseModel):
    state: Literal["unread", "read", "interested", "ignored"]


class CatalogEventBulkUpdate(BaseModel):
    state: Literal["read", "ignored"] = "read"
    author_id: int | None = Field(default=None, ge=1)
