from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.time import utc_timestamp
from app.db.models import (
    Author,
    AuthorWork,
    MergeSuggestion,
    Work,
    WorkGroup,
    WorkGroupMember,
    WorkSource,
)
from app.modules.catalog.schemas import WorkGroupRead
from app.modules.media.urls import _PROXIED_PROVIDERS


def _chunked(values: list[int], size: int = 500) -> Iterable[list[int]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


class WorkGroupRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def list_groups(self, author_id: int | None = None) -> list[WorkGroup]:
        statement = select(WorkGroup).options(
            selectinload(WorkGroup.members)
            .selectinload(WorkGroupMember.work)
            .selectinload(Work.sources),
            selectinload(WorkGroup.members)
            .selectinload(WorkGroupMember.work)
            .selectinload(Work.fingerprint),
            selectinload(WorkGroup.members)
            .selectinload(WorkGroupMember.work)
            .selectinload(Work.authors),
        )
        if author_id is not None:
            statement = (
                statement.join(WorkGroupMember)
                .join(AuthorWork, AuthorWork.work_id == WorkGroupMember.work_id)
                .where(AuthorWork.author_id == author_id)
                .distinct()
            )
        groups = list(self.session.scalars(statement))
        return self._sort_groups(groups)

    def list_group_reads(self, author_id: int | None = None) -> list[WorkGroupRead]:
        """Load work-group list summaries via flat column queries (bypassing selectin JSON)."""
        statement = select(
            WorkGroup.id,
            WorkGroup.title,
            WorkGroup.description,
            WorkGroup.cover_url,
            WorkGroup.status,
            WorkGroup.year,
            WorkGroup.language,
            WorkGroup.tags,
            WorkGroup.first_source_at,
            WorkGroup.latest_source_at,
        )
        if author_id is not None:
            statement = (
                statement.join(WorkGroupMember, WorkGroupMember.group_id == WorkGroup.id)
                .join(AuthorWork, AuthorWork.work_id == WorkGroupMember.work_id)
                .where(AuthorWork.author_id == author_id)
                .distinct()
            )
        rows = list(self.session.execute(statement).all())
        if not rows:
            return []
        rows.sort(key=lambda row: row.title.casefold())
        rows.sort(key=lambda row: self._timestamp(row.first_source_at), reverse=True)
        rows.sort(key=lambda row: row.first_source_at is None)

        group_ids = [row.id for row in rows]
        works_by_group: dict[int, list[tuple[int, str | None]]] = defaultdict(list)
        providers_by_group: dict[int, set[str]] = defaultdict(set)
        proxied_work_ids: set[int] = set()
        authors_by_group: dict[int, set[tuple[int, str]]] = defaultdict(set)

        id_batches: Iterable[list[int] | None] = (
            _chunked(group_ids) if author_id is not None else [None]
        )
        for batch in id_batches:
            member_stmt = (
                select(WorkGroupMember.group_id, Work.id, Work.cover_url)
                .join(Work, Work.id == WorkGroupMember.work_id)
                .order_by(WorkGroupMember.group_id, WorkGroupMember.work_id)
            )
            source_stmt = (
                select(
                    WorkGroupMember.group_id,
                    WorkSource.work_id,
                    WorkSource.provider,
                )
                .join(WorkSource, WorkSource.work_id == WorkGroupMember.work_id)
                .distinct()
            )
            author_stmt = (
                select(WorkGroupMember.group_id, Author.id, Author.name)
                .join(AuthorWork, AuthorWork.work_id == WorkGroupMember.work_id)
                .join(Author, Author.id == AuthorWork.author_id)
                .distinct()
            )
            if batch is not None:
                member_stmt = member_stmt.where(WorkGroupMember.group_id.in_(batch))
                source_stmt = source_stmt.where(WorkGroupMember.group_id.in_(batch))
                author_stmt = author_stmt.where(WorkGroupMember.group_id.in_(batch))

            for gid, wid, work_cover in self.session.execute(member_stmt):
                works_by_group[gid].append((wid, work_cover))
            for gid, wid, provider in self.session.execute(source_stmt):
                providers_by_group[gid].add(provider)
                if provider in _PROXIED_PROVIDERS:
                    proxied_work_ids.add(wid)
            for gid, aid, aname in self.session.execute(author_stmt):
                authors_by_group[gid].add((aid, aname))

        reads: list[WorkGroupRead] = []
        for row in rows:
            members = works_by_group.get(row.id, [])
            cover_url = self._resolve_group_cover(row.cover_url, members, proxied_work_ids)
            authors = sorted(
                authors_by_group.get(row.id, ()),
                key=lambda item: item[1].casefold(),
            )
            reads.append(
                WorkGroupRead(
                    id=row.id,
                    title=row.title,
                    description=row.description,
                    cover_url=cover_url,
                    status=row.status,
                    year=row.year,
                    language=row.language,
                    tags=row.tags or [],
                    first_source_at=row.first_source_at,
                    latest_source_at=row.latest_source_at,
                    edition_count=len(members),
                    providers=sorted(providers_by_group.get(row.id, ())),
                    authors=[{"id": aid, "name": name} for aid, name in authors],
                )
            )
        return reads

    @staticmethod
    def _resolve_group_cover(
        group_cover: str | None,
        members: list[tuple[int, str | None]],
        proxied_work_ids: set[int],
    ) -> str | None:
        if not group_cover:
            return None
        selected = next(
            ((wid, wcover) for wid, wcover in members if wcover == group_cover),
            None,
        )
        if selected is None:
            selected = next(((wid, wcover) for wid, wcover in members if wcover), None)
        if selected is None:
            return group_cover
        wid, wcover = selected
        if not wcover:
            return None
        if wid not in proxied_work_ids:
            return wcover
        version = sha256(wcover.encode()).hexdigest()[:12]
        return f"/api/works/{wid}/cover?v={version}"

    def get(self, group_id: int) -> WorkGroup | None:
        return self.session.scalar(
            select(WorkGroup)
            .options(
                selectinload(WorkGroup.members)
                .selectinload(WorkGroupMember.work)
                .selectinload(Work.sources),
                selectinload(WorkGroup.members)
                .selectinload(WorkGroupMember.work)
                .selectinload(Work.fingerprint),
                selectinload(WorkGroup.members)
                .selectinload(WorkGroupMember.work)
                .selectinload(Work.authors),
            )
            .where(WorkGroup.id == group_id)
        )

    def get_many(self, group_ids: Iterable[int]) -> dict[int, WorkGroup]:
        unique_ids = sorted({int(gid) for gid in group_ids})
        if not unique_ids:
            return {}
        loaded: dict[int, WorkGroup] = {}
        for batch in _chunked(unique_ids):
            rows = self.session.scalars(
                select(WorkGroup)
                .options(
                    selectinload(WorkGroup.members)
                    .selectinload(WorkGroupMember.work)
                    .selectinload(Work.sources),
                    selectinload(WorkGroup.members)
                    .selectinload(WorkGroupMember.work)
                    .selectinload(Work.fingerprint),
                    selectinload(WorkGroup.members)
                    .selectinload(WorkGroupMember.work)
                    .selectinload(Work.authors),
                )
                .where(WorkGroup.id.in_(batch))
            )
            for group in rows:
                loaded[group.id] = group
        return loaded

    def suggestions(self, status: str = "pending") -> list[MergeSuggestion]:
        return list(
            self.session.scalars(
                select(MergeSuggestion)
                .where(MergeSuggestion.status == status)
                .order_by(MergeSuggestion.confidence.desc())
            )
        )

    def suggestion(self, suggestion_id: int) -> MergeSuggestion | None:
        return self.session.get(MergeSuggestion, suggestion_id)

    @classmethod
    def _sort_groups(cls, groups: list[WorkGroup]) -> list[WorkGroup]:
        groups.sort(key=lambda group: group.title.casefold())
        groups.sort(key=lambda group: cls._timestamp(group.first_source_at), reverse=True)
        groups.sort(key=lambda group: group.first_source_at is None)
        return groups

    @staticmethod
    def _timestamp(value: datetime | None) -> float:
        if value is None:
            return 0.0
        return utc_timestamp(value)
