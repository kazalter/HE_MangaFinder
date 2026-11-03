import asyncio
import logging
from datetime import UTC, datetime

from app.core.config import Settings
from app.db.session import SessionLocal
from app.modules.catalog.discovery_repository import DiscoveryRepository
from app.modules.jobs.repository import JobRepository
from app.providers.registry import ProviderRegistry

logger = logging.getLogger(__name__)


class CatalogScheduler:
    """Periodically enqueue stale author subscriptions without duplicating active jobs."""

    def __init__(self, settings: Settings, providers: ProviderRegistry) -> None:
        self.settings = settings
        self.providers = providers

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
            queued_author_ids: set[int] = set()
            for author in due:
                jobs.enqueue_discovery(author.id, trigger="scheduled")
                queued_author_ids.add(author.id)

            retries = DiscoveryRepository(session).due_provider_retries(
                now,
                self.settings.catalog_provider_retry_base_minutes,
                self.settings.catalog_provider_retry_max_minutes,
                {provider.name for provider in self.providers.all()},
            )
            paused = {
                provider.name
                for provider in self.providers.all()
                if (until := DiscoveryRepository(session).provider_pause_until(
                    provider.name,
                    self.settings.catalog_provider_retry_base_minutes,
                    self.settings.catalog_provider_retry_max_minutes,
                )) is not None and until > now
            }
            for author_id, provider_names in retries.items():
                provider_names = [name for name in provider_names if name not in paused]
                if not provider_names:
                    continue
                jobs.enqueue_discovery(
                    author_id,
                    trigger="provider_retry",
                    providers=provider_names,
                )
                queued_author_ids.add(author_id)
            session.commit()
            return len(queued_author_ids)
