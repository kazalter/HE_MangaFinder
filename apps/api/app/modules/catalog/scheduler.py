import asyncio
import logging
from datetime import UTC, datetime

from app.core.config import Settings
from app.db.session import SessionLocal
from app.modules.catalog.discovery_repository import DiscoveryRepository
from app.modules.jobs.repository import JobRepository

logger = logging.getLogger(__name__)


class CatalogScheduler:
    """Periodically enqueue stale author subscriptions without duplicating active jobs."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def run(self) -> None:
        while True:
            try:
                if self.settings.catalog_auto_discovery_enabled:
                    self.enqueue_due(datetime.now(UTC))
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Catalog scheduler failed")
                await asyncio.sleep(60)

    def enqueue_due(self, now: datetime) -> int:
        with SessionLocal() as session:
            due = DiscoveryRepository(session).due_authors(
                now, self.settings.catalog_discovery_interval_minutes
            )
            jobs = JobRepository(session)
            for author in due:
                jobs.enqueue_discovery(author.id, trigger="scheduled")
            session.commit()
            return len(due)
