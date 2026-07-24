from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    Author,
    CatalogEvent,
    DiscoveryRun,
    ProviderScanRun,
    WorkGroup,
)


class DiscoveryRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def start_run(self, author_id: int, trigger: str) -> DiscoveryRun:
        run = DiscoveryRun(author_id=author_id, trigger=trigger, status="running")
        self.session.add(run)
        self.session.flush()
        return run

    def start_provider(
        self, run_id: int, provider: str, query_name: str
    ) -> ProviderScanRun:
        result = ProviderScanRun(
            discovery_run_id=run_id,
            provider=provider,
            query_name=query_name,
            status="running",
        )
        self.session.add(result)
        self.session.flush()
        return result

    def record_event(
        self,
        *,
        author_id: int,
        group: WorkGroup,
        work_id: int,
        provider: str,
        external_id: str,
        title: str,
    ) -> CatalogEvent:
        event_type = "edition_discovered" if len(group.members) > 1 else "work_discovered"
        existing = self.session.scalar(
            select(CatalogEvent).where(
                CatalogEvent.author_id == author_id,
                CatalogEvent.provider == provider,
                CatalogEvent.external_id == external_id,
                CatalogEvent.event_type == event_type,
            )
        )
        if existing:
            return existing
        event = CatalogEvent(
            author_id=author_id,
            group_id=group.id,
            work_id=work_id,
            provider=provider,
            external_id=external_id,
            event_type=event_type,
            title=title,
        )
        self.session.add(event)
        self.session.flush()
        return event

    def latest_run_times(self) -> dict[int, datetime]:
        rows = self.session.execute(
            select(
                DiscoveryRun.author_id,
                func.max(DiscoveryRun.started_at),
            ).group_by(DiscoveryRun.author_id)
        )
        return {author_id: started_at for author_id, started_at in rows if started_at}

    def due_authors(self, now: datetime, interval_minutes: int) -> list[Author]:
        threshold = now - timedelta(minutes=interval_minutes)
        latest = self.latest_run_times()
        authors = list(self.session.scalars(select(Author).order_by(Author.id)))
        return [
            author
            for author in authors
            if max(
                (value for value in (author.last_checked_at, latest.get(author.id)) if value),
                default=datetime.min.replace(tzinfo=UTC),
            )
            <= threshold
        ]
