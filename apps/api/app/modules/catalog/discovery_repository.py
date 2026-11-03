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

    def recover_interrupted(self, now: datetime) -> int:
        """Finish scans abandoned by an interrupted single-worker process."""
        runs = list(
            self.session.scalars(select(DiscoveryRun).where(DiscoveryRun.status == "running"))
        )
        for run in runs:
            scans = list(
                self.session.scalars(
                    select(ProviderScanRun).where(ProviderScanRun.discovery_run_id == run.id)
                )
            )
            for scan in scans:
                if scan.status == "running":
                    scan.status = "failed"
                    scan.error = "服务重启中断了来源扫描，将按重试策略重新检查"
                    scan.finished_at = now
            run.status = (
                "partial" if any(scan.status == "succeeded" for scan in scans) else "failed"
            )
            run.error = "服务重启中断了本次扫描"
            run.finished_at = now
        if runs:
            self.session.commit()
        return len(runs)

    def start_run(self, author_id: int, trigger: str) -> DiscoveryRun:
        run = DiscoveryRun(author_id=author_id, trigger=trigger, status="running")
        self.session.add(run)
        self.session.flush()
        return run

    def start_provider(self, run_id: int, provider: str, query_name: str) -> ProviderScanRun:
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
            )
            .where(DiscoveryRun.trigger != "provider_retry")
            .group_by(DiscoveryRun.author_id)
        )
        return {author_id: started_at for author_id, started_at in rows if started_at}

    def provider_pause_until(
        self, provider: str, base_delay_minutes: int, max_delay_minutes: int
    ) -> datetime | None:
        """Pause repeated source-wide failures; a later successful scan resets the circuit."""
        scans = self.session.execute(
            select(ProviderScanRun.status, ProviderScanRun.finished_at, ProviderScanRun.error)
            .where(
                ProviderScanRun.provider == provider,
                ProviderScanRun.status.in_(["succeeded", "failed"]),
                ProviderScanRun.finished_at.is_not(None),
            )
            .order_by(ProviderScanRun.finished_at.desc(), ProviderScanRun.id.desc())
            .limit(64)
        )
        count = 0
        latest = None
        for status, finished_at, error in scans:
            # Process restarts say nothing about the source's actual health.
            if error and "服务重启" in error:
                continue
            if status == "succeeded":
                break
            if latest is None:
                latest = finished_at
            count += 1
        if count < 3 or latest is None:
            return None
        base = max(1, base_delay_minutes)
        maximum = max(base, max_delay_minutes)
        delay = min(base * (1 << min(count - 3, 12)), maximum)
        return latest + timedelta(minutes=delay)

    def due_provider_retries(
        self,
        now: datetime,
        base_delay_minutes: int,
        max_delay_minutes: int,
        enabled_providers: set[str],
    ) -> dict[int, list[str]]:
        """Return only failed author/provider pairs whose exponential delay elapsed."""
        rows = self.session.execute(
            select(
                DiscoveryRun.author_id,
                ProviderScanRun.provider,
                ProviderScanRun.status,
                ProviderScanRun.finished_at,
            )
            .join(
                DiscoveryRun,
                DiscoveryRun.id == ProviderScanRun.discovery_run_id,
            )
            .where(ProviderScanRun.provider.in_(enabled_providers))
            .order_by(
                DiscoveryRun.author_id,
                ProviderScanRun.provider,
                ProviderScanRun.started_at.desc(),
                ProviderScanRun.id.desc(),
            )
        )
        failures: dict[tuple[int, str], tuple[int, datetime]] = {}
        completed: set[tuple[int, str]] = set()
        for author_id, provider, status, finished_at in rows:
            key = (author_id, provider)
            if key in completed:
                continue
            if status != "failed" or finished_at is None:
                completed.add(key)
                continue
            count, latest_failure = failures.get(key, (0, finished_at))
            failures[key] = (count + 1, latest_failure)

        due: dict[int, list[str]] = {}
        base_delay_minutes = max(base_delay_minutes, 1)
        max_delay_minutes = max(max_delay_minutes, base_delay_minutes)
        max_shift = (max_delay_minutes // base_delay_minutes).bit_length()
        for (author_id, provider), (failure_count, latest_failure) in failures.items():
            shift = min(max(failure_count - 1, 0), max_shift)
            delay = min(base_delay_minutes * (1 << shift), max_delay_minutes)
            if latest_failure + timedelta(minutes=delay) <= now:
                due.setdefault(author_id, []).append(provider)
        return due

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
