from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    ActivityItem,
    Author,
    AuthorWork,
    CatalogEvent,
    DiscoveryRun,
    PlannedRelease,
    ReleaseSignal,
    SocialAccount,
    SocialPost,
    Work,
    WorkGroup,
    WorkGroupMember,
    WorkSource,
)


@dataclass
class AssistantContext:
    data: dict[str, object]
    citations: list[dict[str, str]]
    work_groups: dict[int, WorkGroup]
    planned_releases: dict[int, PlannedRelease]


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _normalize(value: str) -> str:
    return "".join(char.casefold() for char in value if char.isalnum())


def _safe_url(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return value


def _short(value: str | None, limit: int = 500) -> str:
    return " ".join((value or "").split())[:limit]


class AssistantRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def _match_author(self, question: str, authors: list[Author]) -> Author | None:
        normalized_question = _normalize(question)
        candidates: list[tuple[int, Author]] = []
        for author in authors:
            names = [author.name, *(author.aliases or []), *(author.circle_names or [])]
            for name in names:
                normalized_name = _normalize(name)
                if len(normalized_name) >= 2 and normalized_name in normalized_question:
                    candidates.append((len(normalized_name), author))
        account_rows = list(
            self.session.scalars(
                select(SocialAccount).where(
                    SocialAccount.platform == "x",
                    SocialAccount.status == "confirmed",
                )
            )
        )
        author_by_id = {author.id: author for author in authors}
        for account in account_rows:
            normalized_handle = _normalize(account.handle)
            if (
                len(normalized_handle) >= 2
                and normalized_handle in normalized_question
                and account.author_id in author_by_id
            ):
                candidates.append((len(normalized_handle), author_by_id[account.author_id]))
        if not candidates:
            return None
        longest = max(length for length, _ in candidates)
        matches = {author.id: author for length, author in candidates if length == longest}
        return next(iter(matches.values())) if len(matches) == 1 else None

    def build_context(self, question: str) -> AssistantContext:
        now = datetime.now(UTC)
        authors = list(self.session.scalars(select(Author).order_by(Author.name)))
        author = self._match_author(question, authors)
        scope_author_id = author.id if author else None
        author_names = {item.id: item.name for item in authors}
        citations: list[dict[str, str]] = []
        citation_index_by_url: dict[str, int] = {}

        def citation(label: str, source_url: str | None) -> int | None:
            url = _safe_url(source_url)
            if not url:
                return None
            if url in citation_index_by_url:
                return citation_index_by_url[url]
            index = len(citations)
            citations.append({"label": label[:160], "url": url})
            citation_index_by_url[url] = index
            return index

        if scope_author_id is not None:
            group_ids = select(WorkGroupMember.group_id).join(
                AuthorWork, AuthorWork.work_id == WorkGroupMember.work_id
            ).where(AuthorWork.author_id == scope_author_id)
            groups = list(
                self.session.scalars(
                    select(WorkGroup)
                    .where(WorkGroup.id.in_(group_ids))
                    .order_by(WorkGroup.latest_source_at.desc(), WorkGroup.updated_at.desc())
                    .limit(20)
                )
            )
            signals = list(
                self.session.scalars(
                    select(ReleaseSignal)
                    .where(ReleaseSignal.author_id == scope_author_id)
                    .order_by(ReleaseSignal.updated_at.desc(), ReleaseSignal.id.desc())
                    .limit(16)
                )
            )
            planned = list(
                self.session.scalars(
                    select(PlannedRelease)
                    .where(PlannedRelease.author_id == scope_author_id)
                    .order_by(PlannedRelease.updated_at.desc(), PlannedRelease.id.desc())
                    .limit(12)
                )
            )
            events = list(
                self.session.scalars(
                    select(CatalogEvent)
                    .where(CatalogEvent.author_id == scope_author_id)
                    .order_by(CatalogEvent.created_at.desc(), CatalogEvent.id.desc())
                    .limit(12)
                )
            )
            activities = list(
                self.session.scalars(
                    select(ActivityItem)
                    .where(ActivityItem.author_id == scope_author_id)
                    .order_by(ActivityItem.ended_at.desc(), ActivityItem.id.desc())
                    .limit(10)
                )
            )
            runs = list(
                self.session.scalars(
                    select(DiscoveryRun)
                    .where(DiscoveryRun.author_id == scope_author_id)
                    .order_by(DiscoveryRun.started_at.desc(), DiscoveryRun.id.desc())
                    .limit(4)
                )
            )
        else:
            groups = list(
                self.session.scalars(
                    select(WorkGroup)
                    .order_by(WorkGroup.latest_source_at.desc(), WorkGroup.updated_at.desc())
                    .limit(20)
                )
            )
            signals = list(
                self.session.scalars(
                    select(ReleaseSignal)
                    .order_by(ReleaseSignal.updated_at.desc(), ReleaseSignal.id.desc())
                    .limit(20)
                )
            )
            planned = list(
                self.session.scalars(
                    select(PlannedRelease)
                    .order_by(PlannedRelease.updated_at.desc(), PlannedRelease.id.desc())
                    .limit(12)
                )
            )
            events = list(
                self.session.scalars(
                    select(CatalogEvent)
                    .order_by(CatalogEvent.created_at.desc(), CatalogEvent.id.desc())
                    .limit(14)
                )
            )
            activities = list(
                self.session.scalars(
                    select(ActivityItem)
                    .order_by(ActivityItem.ended_at.desc(), ActivityItem.id.desc())
                    .limit(12)
                )
            )
            runs = list(
                self.session.scalars(
                    select(DiscoveryRun)
                    .order_by(DiscoveryRun.started_at.desc(), DiscoveryRun.id.desc())
                    .limit(6)
                )
            )

        normalized_question = _normalize(question)
        matching_group_rows = self.session.execute(
            select(WorkGroup.id, WorkGroup.title)
        ).all()
        matching_group_ids = [
            group_id
            for group_id, title in matching_group_rows
            if len(_normalize(title)) >= 2 and _normalize(title) in normalized_question
        ][:5]
        if matching_group_ids:
            matched_groups = [
                group
                for group_id in matching_group_ids
                if (group := self.session.get(WorkGroup, group_id)) is not None
            ]
            existing_ids = {group.id for group in matched_groups}
            groups = matched_groups + [
                group for group in groups if group.id not in existing_ids
            ]
            groups = groups[:25]

        matching_planned_rows = self.session.execute(
            select(PlannedRelease.id, PlannedRelease.title)
        ).all()
        matching_planned_ids = [
            planned_id
            for planned_id, title in matching_planned_rows
            if len(_normalize(title)) >= 2 and _normalize(title) in normalized_question
        ][:5]
        if matching_planned_ids:
            matched_planned = [
                item
                for planned_id in matching_planned_ids
                if (item := self.session.get(PlannedRelease, planned_id)) is not None
            ]
            existing_planned_ids = {item.id for item in matched_planned}
            planned = matched_planned + [
                item for item in planned if item.id not in existing_planned_ids
            ]
            planned = planned[:17]

        group_map = {item.id: item for item in groups}
        planned_map = {item.id: item for item in planned}
        group_data: list[dict[str, object]] = []
        for group in groups:
            work_ids = [member.work_id for member in group.members]
            group_authors: list[str] = []
            if work_ids:
                group_authors = sorted(
                    {
                        item.name
                        for item in self.session.scalars(
                            select(Author)
                            .join(AuthorWork, AuthorWork.author_id == Author.id)
                            .where(AuthorWork.work_id.in_(work_ids))
                        )
                    }
                )
            overrides = group.manual_metadata or {}
            safe_overrides = {
                key: (
                    _short(value, 800)
                    if key == "description" and isinstance(value, str)
                    else value
                )
                for key, value in overrides.items()
                if key in {
                    "title", "description", "status", "year", "language", "tags",
                    "cover_work_id", "preferred_work_id",
                }
            }
            group_data.append(
                {
                    "id": group.id,
                    "title": group.title,
                    "description": _short(group.description, 800),
                    "authors": group_authors,
                    "editions": [
                        {
                            "work_id": member.work_id,
                            "title": member.work.title,
                            "has_cover": bool(member.work.cover_url),
                        }
                        for member in group.members
                    ],
                    "status": group.status,
                    "year": group.year,
                    "language": group.language,
                    "tags": overrides.get("tags", group.tags or []),
                    "first_source_at": _iso(group.first_source_at),
                    "latest_source_at": _iso(group.latest_source_at),
                    "updated_at": _iso(group.updated_at),
                    "manual_metadata": safe_overrides,
                }
            )

        signal_data: list[dict[str, object]] = []
        for signal in signals:
            post = self.session.get(SocialPost, signal.primary_post_id)
            post_url = _safe_url(post.url) if post else None
            reference = citation(
                f"{author_names.get(signal.author_id, '作者')}：{signal.title or signal.kind}",
                post_url,
            )
            signal_data.append(
                {
                    "id": signal.id,
                    "author": author_names.get(signal.author_id, "未知作者"),
                    "kind": signal.kind,
                    "title": signal.title,
                    "status": signal.status,
                    "confidence": signal.confidence,
                    "release_date": _iso(signal.release_date),
                    "event_code": signal.event_code,
                    "booth": signal.booth,
                    "updated_at": _iso(signal.updated_at),
                    "post_date": _iso(post.posted_at) if post else None,
                    "post_text": _short(post.text, 600) if post else None,
                    "post_url": post_url,
                    "citation_index": reference,
                }
            )

        event_data: list[dict[str, object]] = []
        for event in events:
            group = group_map.get(event.group_id) if event.group_id else None
            if not group and event.group_id:
                group = self.session.get(WorkGroup, event.group_id)
            work = self.session.get(Work, event.work_id) if event.work_id else None
            source_url = self.session.scalar(
                select(WorkSource.source_url)
                .where(
                    WorkSource.provider == event.provider,
                    WorkSource.external_id == event.external_id,
                )
                .limit(1)
            )
            reference = citation(f"{event.provider}：{event.title}", source_url)
            event_data.append(
                {
                    "id": event.id,
                    "author": author_names.get(event.author_id, "未知作者"),
                    "title": event.title,
                    "event_type": event.event_type,
                    "state": event.state,
                    "provider": event.provider,
                    "discovered_at": _iso(event.created_at),
                    "catalog_title": group.title if group else work.title if work else None,
                    "citation_index": reference,
                }
            )

        activity_data: list[dict[str, object]] = []
        for activity in activities:
            post = self.session.get(SocialPost, activity.primary_post_id)
            post_url = _safe_url(post.url) if post else None
            reference = citation(
                f"{author_names.get(activity.author_id, '作者')}：{activity.headline}",
                post_url,
            )
            activity_data.append(
                {
                    "id": activity.id,
                    "author": author_names.get(activity.author_id, "未知作者"),
                    "category": activity.category,
                    "headline": activity.headline,
                    "summary": _short(activity.summary, 700),
                    "started_at": _iso(activity.started_at),
                    "ended_at": _iso(activity.ended_at),
                    "post_date": _iso(post.posted_at) if post else None,
                    "post_url": post_url,
                    "citation_index": reference,
                }
            )

        planned_data: list[dict[str, object]] = []
        for item in planned:
            planned_data.append(
                {
                    "id": item.id,
                    "author": author_names.get(item.author_id, "未知作者"),
                    "title": item.title,
                    "status": item.status,
                    "release_date": _iso(item.release_date),
                    "event_code": item.event_code,
                    "booth": item.booth,
                    "linked_group_id": item.linked_group_id,
                    "suggested_group_id": item.suggested_group_id,
                    "updated_at": _iso(item.updated_at),
                }
            )

        run_data = [
            {
                "author": author_names.get(run.author_id, "未知作者"),
                "status": run.status,
                "started_at": _iso(run.started_at),
                "finished_at": _iso(run.finished_at),
                "found_count": run.found_count,
                "new_count": run.new_count,
                "updated_count": run.updated_count,
            }
            for run in runs
        ]

        if author:
            author_scope: dict[str, object] | str = {
                "id": author.id,
                "name": author.name,
                "aliases": author.aliases or [],
                "circle_names": author.circle_names or [],
                "last_catalog_check": _iso(author.last_checked_at),
            }
        else:
            author_scope = "all subscribed authors; no exact author mention matched"

        data: dict[str, object] = {
            "snapshot_at": now.isoformat(),
            "scope": author_scope,
            "release_signals": signal_data,
            "catalog_discoveries": event_data,
            "author_activity": activity_data,
            "planned_releases": planned_data,
            "catalog_works": group_data,
            "recent_discovery_runs": run_data,
        }
        return AssistantContext(
            data=data,
            citations=citations,
            work_groups=group_map,
            planned_releases=planned_map,
        )
