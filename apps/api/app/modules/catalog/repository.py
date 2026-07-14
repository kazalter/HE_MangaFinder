from collections import Counter
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db.models import AuthorWork, Work, WorkSource
from app.providers.base import DiscoveredWork


class CatalogRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert(self, author_id: int, provider: str, item: DiscoveredWork) -> Work:
        source = self.session.scalar(
            select(WorkSource)
            .options(selectinload(WorkSource.work))
            .where(
                WorkSource.provider == provider,
                WorkSource.external_id == item.external_id,
            )
        )
        if source is None:
            work = Work(title=item.title)
            source = WorkSource(
                provider=provider,
                external_id=item.external_id,
                source_url=item.source_url,
                work=work,
            )
            self.session.add(work)
        else:
            work = source.work

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
        if link is None:
            self.session.add(AuthorWork(author_id=author_id, work_id=work.id))
        return work

    def get(self, work_id: int) -> Work | None:
        return self.session.scalar(
            select(Work)
            .options(selectinload(Work.sources))
            .where(Work.id == work_id)
        )

    def preferred_author_query_name(
        self, author_id: int, provider: str, fallback: str
    ) -> str:
        """Reuse a dominant provider-native artist tag learned from prior scans."""

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
            return fallback
        ranked = counts.most_common(2)
        identity, support = ranked[0]
        if support < 2 or (len(ranked) > 1 and ranked[1][1] == support):
            return fallback
        return display_names[identity]

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
    def _latest_source_sort_key(
        cls, row: tuple[Work, datetime]
    ) -> tuple[int, float]:
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
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.timestamp()

    def get_source(self, work_id: int, provider: str) -> WorkSource | None:
        return self.session.scalar(
            select(WorkSource)
            .options(selectinload(WorkSource.work))
            .where(WorkSource.work_id == work_id, WorkSource.provider == provider)
        )
