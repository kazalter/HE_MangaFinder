from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol


class ProviderCapability(StrEnum):
    AUTHOR_DISCOVERY = "author_discovery"
    CHAPTER_LIST = "chapter_list"
    DOWNLOAD = "download"


@dataclass(frozen=True, slots=True)
class DiscoveredWork:
    external_id: str
    title: str
    source_url: str
    description: str | None = None
    cover_url: str | None = None
    status: str | None = None
    year: int | None = None
    language: str | None = None
    tags: list[str] = field(default_factory=list)
    source_updated_at: datetime | None = None
    raw_metadata: dict[str, Any] = field(default_factory=dict)


def sort_discovered_works(works: list[DiscoveredWork]) -> list[DiscoveredWork]:
    """Return source results newest-first, with undated works at the end."""

    def key(work: DiscoveredWork) -> tuple[int, float, str]:
        updated_at = work.source_updated_at
        if updated_at is None:
            return (1, 0.0, work.title.casefold())
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=UTC)
        return (0, -updated_at.timestamp(), work.title.casefold())

    return sorted(works, key=key)


@dataclass(frozen=True, slots=True)
class Chapter:
    external_id: str
    title: str | None
    number: str | None
    language: str
    published_at: datetime | None
    source_url: str


@dataclass(frozen=True, slots=True)
class RemoteImage:
    content: bytes
    content_type: str


class SourceProvider(Protocol):
    name: str
    display_name: str
    capabilities: frozenset[ProviderCapability]

    async def discover_by_author(self, author_name: str) -> list[DiscoveredWork]: ...

    async def list_chapters(self, work_external_id: str) -> list[Chapter]: ...

    async def download_chapter(
        self, work_external_id: str, chapter_external_id: str, destination: str
    ) -> str: ...

    async def fetch_cover(
        self, work_external_id: str, cover_url: str
    ) -> RemoteImage: ...

    async def close(self) -> None: ...
