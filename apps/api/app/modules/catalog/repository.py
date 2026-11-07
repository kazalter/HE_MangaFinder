from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.time import utc_timestamp
from app.db.models import (
    Author,
    AuthorIdentitySuggestion,
    AuthorWork,
    AuthorWorkExclusion,
    Work,
    WorkSource,
)
from app.providers.base import DiscoveredWork


@dataclass(frozen=True)
class CatalogUpsertResult:
    work: Work
    source_created: bool
    author_link_created: bool
    metadata_changed: bool


@dataclass(frozen=True, slots=True)
class AuthorQueryNameDecision:
    query_name: str
    origin: str
    explanation: str
    support: int = 0


class CatalogRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert(self, author_id: int, provider: str, item: DiscoveredWork) -> Work:
        return self.upsert_with_result(author_id, provider, item).work

    def upsert_with_result(
        self, author_id: int, provider: str, item: DiscoveredWork
    ) -> CatalogUpsertResult:
        source = self.session.scalar(
            select(WorkSource)
            .options(selectinload(WorkSource.work))
            .where(
                WorkSource.provider == provider,
                WorkSource.external_id == item.external_id,
            )
        )
        source_created = source is None
        if source is None:
            work = Work(title=item.title)
            source = WorkSource(
                provider=provider,
                external_id=item.external_id,
                source_url=item.source_url,
                work=work,
            )
            self.session.add(work)
            metadata_changed = False
        else:
            work = source.work
            metadata_changed = (
                work.title != item.title
                or work.description != item.description
                or work.cover_url != item.cover_url
                or work.status != item.status
                or work.year != item.year
                or work.language != item.language
                or work.tags != item.tags
                or source.source_url != item.source_url
                or source.raw_metadata != item.raw_metadata
                or source.source_updated_at != item.source_updated_at
            )

        work.title = item.title
        work.description = item.description
        work.cover_url = item.cover_url
        work.status = item.status
        work.year = item.year
        work.language = item.language
        work.tags = item.tags
        source.source_url = item.source_url
        source.raw_metadata = item.raw_metadata
        source.source_updated_at = item.source_updated_at
        self.session.flush()

        link = self.session.get(AuthorWork, (author_id, work.id))
        author_link_created = link is None
        if link is None:
            self.session.add(AuthorWork(author_id=author_id, work_id=work.id))
        return CatalogUpsertResult(
            work=work,
            source_created=source_created,
            author_link_created=author_link_created,
            metadata_changed=metadata_changed,
        )

    def get(self, work_id: int) -> Work | None:
        return self.session.scalar(
            select(Work).options(selectinload(Work.sources)).where(Work.id == work_id)
        )

    def excluded_external_ids(self, author_id: int, provider: str) -> set[str]:
        return set(
            self.session.scalars(
                select(AuthorWorkExclusion.external_id).where(
                    AuthorWorkExclusion.author_id == author_id,
                    AuthorWorkExclusion.provider == provider,
                )
            )
        )

    def preferred_author_query_name(self, author_id: int, provider: str, fallback: str) -> str:
        return self.author_query_name_decision(author_id, provider, fallback).query_name

    def author_query_name_decision(
        self, author_id: int, provider: str, fallback: str
    ) -> AuthorQueryNameDecision:
        """Choose an explicit query or explain the conservative historical inference."""

        author = self.session.get(Author, author_id)
        explicit = (author.provider_query_names or {}).get(provider.casefold()) if author else None
        if explicit:
            return AuthorQueryNameDecision(
                query_name=explicit,
                origin="manual",
                explanation=f"使用为 {provider} 人工配置的来源专用查询名",
            )

        known = (
            {
                value.casefold()
                for value in ([author.name] + (author.aliases or []) + (author.circle_names or []))
            }
            if author
            else set()
        )
        suggested = self.session.scalars(
            select(AuthorIdentitySuggestion)
            .where(
                AuthorIdentitySuggestion.author_id == author_id,
                AuthorIdentitySuggestion.provider == provider,
            )
            .order_by(AuthorIdentitySuggestion.id.desc())
        )
        for candidate in suggested:
            if candidate.name.casefold() not in known:
                return AuthorQueryNameDecision(
                    query_name=candidate.name,
                    origin="inferred",
                    explanation=f"{provider} 推测的名称尚未确认；请核对后加入别名",
                    support=0,
                )

        metadata_rows = self.session.scalars(
            select(WorkSource.raw_metadata)
            .join(AuthorWork, AuthorWork.work_id == WorkSource.work_id)
            .where(
                AuthorWork.author_id == author_id,
                WorkSource.provider == provider,
            )
        )
        counts: Counter[str] = Counter()
        display_names: dict[str, str] = {}
        for metadata in metadata_rows:
            artists = metadata.get("artists") if isinstance(metadata, dict) else None
            if not isinstance(artists, list):
                continue
            for value in set(item for item in artists if isinstance(item, str)):
                cleaned = " ".join(value.split()).strip()
                identity = cleaned.casefold()
                if identity:
                    counts[identity] += 1
                    display_names.setdefault(identity, cleaned)
        if not counts:
            return AuthorQueryNameDecision(
                query_name=fallback,
                origin="canonical",
                explanation="尚无可靠来源别名，使用作者标准名",
            )
        ranked = counts.most_common(2)
        identity, support = ranked[0]
        if support < 2 or (len(ranked) > 1 and ranked[1][1] == support):
            return AuthorQueryNameDecision(
                query_name=fallback,
                origin="canonical",
                explanation="来源历史别名证据不足或存在并列冲突，使用作者标准名",
                support=support,
            )
        return AuthorQueryNameDecision(
            query_name=display_names[identity],
            origin="inferred",
            explanation=f"从 {support} 条 {provider} 历史作品推测；需确认后作为别名使用",
            support=support,
        )

    def list_for_author(self, author_id: int | None = None) -> list[tuple[Work, datetime]]:
        statement = (
            select(Work, AuthorWork)
            .join(AuthorWork, AuthorWork.work_id == Work.id)
            .options(selectinload(Work.sources))
            .order_by(AuthorWork.discovered_at.desc(), Work.title)
        )
        if author_id is not None:
            statement = statement.where(AuthorWork.author_id == author_id)
        rows = self.session.execute(statement).all()
        unique: dict[int, tuple[Work, datetime]] = {}
        for work, link in rows:
            unique.setdefault(work.id, (work, link.discovered_at))
        result = list(unique.values())

        # Stable sorts keep title deterministic, then prefer recently discovered
        # works for ties, and finally make the newest dated source authoritative.
        result.sort(key=lambda row: row[0].title.casefold())
        result.sort(key=lambda row: self._timestamp(row[1]), reverse=True)
        result.sort(key=self._latest_source_sort_key)
        return result

    @classmethod
    def _latest_source_sort_key(cls, row: tuple[Work, datetime]) -> tuple[int, float]:
        timestamps = [
            cls._timestamp(source.source_updated_at)
            for source in row[0].sources
            if source.source_updated_at is not None
        ]
        if not timestamps:
            return (1, 0.0)
        return (0, -max(timestamps))

    @staticmethod
    def _timestamp(value: datetime) -> float:
        return utc_timestamp(value)

    def get_source(self, work_id: int, provider: str) -> WorkSource | None:
        return self.session.scalar(
            select(WorkSource)
            .options(selectinload(WorkSource.work))
            .where(WorkSource.work_id == work_id, WorkSource.provider == provider)
        )
