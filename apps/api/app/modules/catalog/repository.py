from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.time import as_utc, utc_timestamp
from app.db.models import (
    Author,
    AuthorIdentitySuggestion,
    AuthorWork,
    AuthorWorkExclusion,
    Work,
    WorkSource,
)
from app.providers.base import DiscoveredWork

_MIN_UTC = datetime.min.replace(tzinfo=UTC)


@dataclass(frozen=True)
class CatalogUpsertResult:
    work: Work
    source_created: bool
    author_link_created: bool
    metadata_changed: bool


@dataclass(frozen=True, slots=True)
class DetailPlan:
    fetch_ids: set[str]
    cache_hits: int


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

    @staticmethod
    def _merge_raw_metadata(
        existing: dict[str, Any] | None, item: DiscoveredWork
    ) -> dict[str, Any]:
        """Merge provided raw_metadata keys without clobbering richer cached detail keys."""
        if item.raw_metadata_provided_keys is None:
            return dict(item.raw_metadata)
        merged = dict(existing or {})
        for key in item.raw_metadata_provided_keys:
            if key in item.raw_metadata:
                merged[key] = item.raw_metadata[key]
            else:
                merged.pop(key, None)
        for key, value in item.raw_metadata.items():
            if key not in merged:
                merged[key] = value
        return merged

    def upsert_with_result(
        self,
        author_id: int,
        provider: str,
        item: DiscoveredWork,
        *,
        now: datetime | None = None,
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
        ts = now or datetime.now(UTC)
        if source is None:
            work = Work(
                title=item.title,
                description=item.description,
                cover_url=item.cover_url,
                status=item.status,
                year=item.year,
                language=item.language,
                tags=list(item.tags),
            )
            source = WorkSource(
                provider=provider,
                external_id=item.external_id,
                source_url=item.source_url,
                raw_metadata=dict(item.raw_metadata),
                source_updated_at=item.source_updated_at,
                work=work,
            )
            self.session.add(work)
            metadata_changed = False
        else:
            work = source.work
            provided = item.provided_fields
            def _is_provided(field: str) -> bool:
                return provided is None or field in provided

            new_description = item.description if _is_provided("description") else work.description
            new_cover_url = item.cover_url if _is_provided("cover_url") else work.cover_url
            new_status = item.status if _is_provided("status") else work.status
            new_year = item.year if _is_provided("year") else work.year
            new_language = item.language if _is_provided("language") else work.language
            new_tags = list(item.tags) if _is_provided("tags") else list(work.tags or [])
            new_source_updated_at = (
                item.source_updated_at
                if _is_provided("source_updated_at")
                else source.source_updated_at
            )
            new_raw_metadata = (
                self._merge_raw_metadata(source.raw_metadata, item)
                if _is_provided("raw_metadata")
                else dict(source.raw_metadata or {})
            )
            metadata_changed = (
                work.title != item.title
                or work.description != new_description
                or work.cover_url != new_cover_url
                or work.status != new_status
                or work.year != new_year
                or work.language != new_language
                or list(work.tags or []) != new_tags
                or source.source_url != item.source_url
                or source.raw_metadata != new_raw_metadata
                or source.source_updated_at != new_source_updated_at
            )
            work.title = item.title
            work.description = new_description
            work.cover_url = new_cover_url
            work.status = new_status
            work.year = new_year
            work.language = new_language
            work.tags = new_tags
            source.source_url = item.source_url
            source.raw_metadata = new_raw_metadata
            source.source_updated_at = new_source_updated_at

        if item.detail_fetched:
            source.detail_fetched_at = ts
            if item.list_signature is not None:
                source.list_signature = item.list_signature
            source.detail_failure_count = 0
            source.detail_next_retry_at = None
            source.detail_last_attempted_at = ts
        elif item.detail_failed:
            # Never extend detail TTL or mask a changed list signature on failure.
            source.detail_failure_count = (source.detail_failure_count or 0) + 1
            source.detail_last_attempted_at = ts
            delay_seconds = min(60 * (1 << min(source.detail_failure_count - 1, 5)), 3600)
            source.detail_next_retry_at = ts + timedelta(seconds=delay_seconds)
        elif item.is_detail_complete:
            if item.list_signature is not None:
                source.list_signature = item.list_signature
            if source.detail_fetched_at is None:
                source.detail_fetched_at = ts

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

    def plan_detail_fetches(
        self,
        provider: str,
        items: list[DiscoveredWork],
        *,
        budget: int,
        ttl_hours: int,
        force: bool = False,
        now: datetime | None = None,
    ) -> DetailPlan:
        """Select up to `budget` works needing detail fetch with backlog rotation."""
        if not items:
            return DetailPlan(fetch_ids=set(), cache_hits=0)
        current_now = as_utc(now) if now is not None else datetime.now(UTC)
        ttl_delta = timedelta(hours=max(1, ttl_hours))
        external_ids = [item.external_id for item in items]
        rows = {
            row.external_id: row
            for row in self.session.scalars(
                select(WorkSource).where(
                    WorkSource.provider == provider,
                    WorkSource.external_id.in_(external_ids),
                )
            )
        }
        prioritized: list[tuple[tuple[Any, ...], str]] = []
        cache_hits = 0
        for index, item in enumerate(items):
            if item.is_detail_complete:
                continue
            row = rows.get(item.external_id)
            if row is None or row.detail_fetched_at is None:
                failures = row.detail_failure_count if row is not None else 0
                if failures == 0:
                    prioritized.append(((0, 0, _MIN_UTC, index), item.external_id))
                else:
                    last_attempt = (
                        as_utc(row.detail_last_attempted_at)
                        if row.detail_last_attempted_at is not None
                        else _MIN_UTC
                    )
                    prioritized.append(((2, failures, last_attempt, index), item.external_id))
                continue

            signature_changed = (
                item.list_signature is not None and row.list_signature != item.list_signature
            )
            fetched_at = as_utc(row.detail_fetched_at)
            expired = fetched_at + ttl_delta <= current_now
            if signature_changed:
                failures = row.detail_failure_count or 0
                if failures == 0:
                    prioritized.append(((1, 0, _MIN_UTC, index), item.external_id))
                else:
                    last_attempt = (
                        as_utc(row.detail_last_attempted_at)
                        if row.detail_last_attempted_at is not None
                        else _MIN_UTC
                    )
                    prioritized.append(((2, failures, last_attempt, index), item.external_id))
            elif expired or force:
                prioritized.append(((3, 0, fetched_at, index), item.external_id))
            else:
                cache_hits += 1

        prioritized.sort(key=lambda entry: entry[0])
        selected = {external_id for _, external_id in prioritized[: max(0, budget)]}
        return DetailPlan(fetch_ids=selected, cache_hits=cache_hits)

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
