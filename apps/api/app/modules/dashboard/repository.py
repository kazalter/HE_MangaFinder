from collections import defaultdict
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.core.config import get_settings
from app.db.models import (
    Author,
    CatalogEvent,
    DiscoveryRun,
    MergeSuggestion,
    PlannedRelease,
    ProviderScanRun,
    ReleaseSignal,
    Work,
    WorkGroup,
    WorkGroupMember,
)
from app.modules.catalog.discovery_repository import DiscoveryRepository
from app.modules.dashboard.schemas import (
    CatalogEventRead,
    DashboardRead,
    DashboardSummary,
    SourceHealth,
    TrendPoint,
    UpcomingRelease,
)
from app.modules.media.urls import group_cover_url
from app.providers.errors import classify_provider_error


class DashboardRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def dashboard(self, period_days: int) -> DashboardRead:
        now = datetime.now(UTC)
        since = now - timedelta(days=period_days)
        event_rows = list(
            self.session.scalars(
                select(CatalogEvent).where(CatalogEvent.created_at >= since)
            )
        )
        new_works = sum(item.event_type == "work_discovered" for item in event_rows)
        new_editions = sum(item.event_type == "edition_discovered" for item in event_rows)
        summary = DashboardSummary(
            authors=self._count(Author),
            works=self._count(WorkGroup),
            editions=self._count(Work),
            new_works=new_works,
            new_editions=new_editions,
            unread_discoveries=self._count_where(
                CatalogEvent, CatalogEvent.state == "unread"
            ),
            pending_merges=self._count_where(
                MergeSuggestion, MergeSuggestion.status == "pending"
            ),
            pending_signals=self._count_where(
                ReleaseSignal, ReleaseSignal.status == "pending"
            ),
            failed_provider_scans=self._count_where(
                ProviderScanRun,
                ProviderScanRun.status == "failed",
                ProviderScanRun.started_at >= since,
            ),
            incomplete_works=self._count_where(
                WorkGroup,
                or_(
                    WorkGroup.cover_url.is_(None),
                    WorkGroup.year.is_(None),
                    WorkGroup.language.is_(None),
                ),
            ),
        )
        return DashboardRead(
            period_days=period_days,
            summary=summary,
            trend=self._trend(event_rows, now, period_days),
            source_health=self._source_health(since),
            upcoming=self._upcoming(now),
        )

    def inbox(
        self,
        *,
        state: str | None,
        author_id: int | None,
        limit: int,
    ) -> list[CatalogEventRead]:
        statement = (
            select(CatalogEvent, Author, WorkGroup)
            .join(Author, Author.id == CatalogEvent.author_id)
            .outerjoin(WorkGroup, WorkGroup.id == CatalogEvent.group_id)
            .options(
                selectinload(WorkGroup.members).selectinload(WorkGroupMember.work)
            )
            .order_by(CatalogEvent.created_at.desc(), CatalogEvent.id.desc())
            .limit(limit)
        )
        if state and state != "all":
            statement = statement.where(CatalogEvent.state == state)
        if author_id is not None:
            statement = statement.where(CatalogEvent.author_id == author_id)
        result: list[CatalogEventRead] = []
        for event, author, group in self.session.execute(statement):
            result.append(
                CatalogEventRead(
                    id=event.id,
                    author_id=event.author_id,
                    author_name=author.name,
                    group_id=event.group_id,
                    work_id=event.work_id,
                    provider=event.provider,
                    external_id=event.external_id,
                    event_type=event.event_type,
                    title=event.title,
                    cover_url=group_cover_url(group) if group else None,
                    state=event.state,
                    created_at=event.created_at,
                    read_at=event.read_at,
                )
            )
        return result

    def set_event_state(self, event_id: int, state: str) -> CatalogEvent | None:
        event = self.session.get(CatalogEvent, event_id)
        if event is None:
            return None
        event.state = state
        event.read_at = None if state == "unread" else datetime.now(UTC)
        self.session.flush()
        return event

    def set_all_event_states(self, state: str, author_id: int | None = None) -> int:
        statement = select(CatalogEvent).where(CatalogEvent.state == "unread")
        if author_id is not None:
            statement = statement.where(CatalogEvent.author_id == author_id)
        events = list(self.session.scalars(statement))
        now = datetime.now(UTC)
        for event in events:
            event.state = state
            event.read_at = now
        self.session.flush()
        return len(events)

    def _source_health(self, since: datetime) -> list[SourceHealth]:
        rows = list(
            self.session.execute(
                select(ProviderScanRun, DiscoveryRun.author_id)
                .join(DiscoveryRun, ProviderScanRun.discovery_run_id == DiscoveryRun.id)
                .where(ProviderScanRun.started_at >= since)
                .order_by(ProviderScanRun.started_at.desc(), ProviderScanRun.id.desc())
            )
        )
        grouped: dict[str, list[ProviderScanRun]] = defaultdict(list)
        current: dict[str, dict[int, ProviderScanRun]] = defaultdict(dict)
        for row, author_id in rows:
            grouped[row.provider].append(row)
            if row.status in {"succeeded", "failed"}:
                current[row.provider].setdefault(author_id, row)
        result: list[SourceHealth] = []
        for provider, scans in sorted(grouped.items()):
            succeeded = [item for item in scans if item.status == "succeeded"]
            failed = [item for item in scans if item.status == "failed"]
            finished = succeeded + failed
            current_scans = list(current[provider].values())
            current_failures = [item for item in current_scans if item.status == "failed"]
            last_error = next((item.error for item in current_failures if item.error), None)
            categories: dict[str, int] = {}
            for item in failed:
                category = classify_provider_error(item.error)
                categories[category] = categories.get(category, 0) + 1
            settings = get_settings()
            pause_until = DiscoveryRepository(self.session).provider_pause_until(
                provider,
                settings.catalog_provider_retry_base_minutes,
                settings.catalog_provider_retry_max_minutes,
            )
            result.append(
                SourceHealth(
                    provider=provider,
                    scans=len(finished),
                    succeeded=len(succeeded),
                    failed=len(failed),
                    success_rate=(len(succeeded) / len(finished)) if finished else 0,
                    new_items=sum(item.new_count for item in scans),
                    average_duration_ms=(
                        round(sum(item.duration_ms for item in finished) / len(finished))
                        if finished
                        else 0
                    ),
                    last_success_at=next(
                        (item.finished_at for item in scans if item.status == "succeeded"),
                        None,
                    ),
                    last_error=last_error,
                    current_failed_authors=len(current_failures),
                    current_checked_authors=len(current_scans),
                    failure_categories=categories,
                    retry_paused_until=pause_until,
                )
            )
        return result

    def _upcoming(self, now: datetime) -> list[UpcomingRelease]:
        rows = self.session.execute(
            select(PlannedRelease, Author)
            .join(Author, Author.id == PlannedRelease.author_id)
            .where(
                PlannedRelease.release_date.is_not(None),
                PlannedRelease.release_date >= now - timedelta(days=1),
                PlannedRelease.status != "cancelled",
            )
            .order_by(PlannedRelease.release_date, PlannedRelease.id)
            .limit(20)
        )
        return [
            UpcomingRelease(
                id=planned.id,
                author_id=planned.author_id,
                author_name=author.name,
                title=planned.title,
                kind="planned_release",
                status=planned.status,
                release_date=planned.release_date,
                event_code=planned.event_code,
                booth=planned.booth,
                linked_group_id=planned.linked_group_id,
            )
            for planned, author in rows
            if planned.release_date is not None
        ]

    @staticmethod
    def _trend(
        events: list[CatalogEvent], now: datetime, period_days: int
    ) -> list[TrendPoint]:
        dates = [
            (now - timedelta(days=offset)).date().isoformat()
            for offset in reversed(range(period_days))
        ]
        values = {date: {"works": 0, "editions": 0} for date in dates}
        for event in events:
            key = event.created_at.date().isoformat()
            if key not in values:
                continue
            field = "works" if event.event_type == "work_discovered" else "editions"
            values[key][field] += 1
        return [TrendPoint(date=date, **values[date]) for date in dates]

    def _count(self, model: type) -> int:
        return int(self.session.scalar(select(func.count()).select_from(model)) or 0)

    def _count_where(self, model: type, *conditions: object) -> int:
        return int(
            self.session.scalar(
                select(func.count()).select_from(model).where(*conditions)
            )
            or 0
        )
