import logging
from datetime import UTC, datetime
from time import monotonic

from sqlalchemy.orm import Session

from app.db.models import DiscoveryRun, ProviderScanRun
from app.modules.authors.repository import AuthorRepository
from app.modules.catalog.aggregation import AggregationService, CoverHasher
from app.modules.catalog.discovery_repository import DiscoveryRepository
from app.modules.catalog.repository import CatalogRepository
from app.providers.base import sort_discovered_works
from app.providers.errors import AuthorNotFoundError
from app.providers.registry import ProviderRegistry

logger = logging.getLogger(__name__)


class DiscoveryService:
    def __init__(self, session: Session, providers: ProviderRegistry) -> None:
        self.session = session
        self.providers = providers

    async def discover_author(
        self, author_id: int, *, trigger: str = "manual"
    ) -> dict[str, int | str]:
        author = AuthorRepository(self.session).get(author_id)
        if author is None:
            return {"run_id": 0, "found": 0, "new": 0, "updated": 0, "excluded": 0}

        discovery = DiscoveryRepository(self.session)
        run = discovery.start_run(author.id, trigger)
        self.session.commit()
        run_id = run.id

        catalog = CatalogRepository(self.session)
        cover_hasher = CoverHasher(providers=self.providers)
        aggregation = AggregationService(self.session, cover_hasher)
        found_total = 0
        new_total = 0
        updated_total = 0
        excluded_total = 0
        errors: list[str] = []
        successful_providers = 0
        providers = list(self.providers.all())
        try:
            for provider in providers:
                query_name = catalog.preferred_author_query_name(
                    author.id, provider.name, author.name
                )
                provider_run = discovery.start_provider(run_id, provider.name, query_name)
                self.session.commit()
                provider_run_id = provider_run.id
                started = monotonic()
                try:
                    try:
                        found = await provider.discover_by_author(query_name)
                    except AuthorNotFoundError:
                        if query_name == author.name:
                            raise
                        query_name = author.name
                        provider_run = self.session.get(ProviderScanRun, provider_run_id)
                        if provider_run:
                            provider_run.query_name = query_name
                            self.session.commit()
                        found = await provider.discover_by_author(query_name)

                    works = sort_discovered_works(found)
                    excluded_ids = catalog.excluded_external_ids(author.id, provider.name)
                    accepted = [
                        work for work in works if work.external_id not in excluded_ids
                    ]
                    provider_new = 0
                    provider_updated = 0
                    for work in accepted:
                        result = catalog.upsert_with_result(
                            author.id, provider.name, work
                        )
                        group = await aggregation.assign(result.work, author)
                        if result.source_created or result.author_link_created:
                            discovery.record_event(
                                author_id=author.id,
                                group=group,
                                work_id=result.work.id,
                                provider=provider.name,
                                external_id=work.external_id,
                                title=result.work.title,
                            )
                            provider_new += 1
                        elif result.metadata_changed:
                            provider_updated += 1

                    found_count = len(works)
                    excluded_count = len(works) - len(accepted)
                    provider_run = self.session.get(ProviderScanRun, provider_run_id)
                    if provider_run:
                        provider_run.status = "succeeded"
                        provider_run.found_count = found_count
                        provider_run.new_count = provider_new
                        provider_run.updated_count = provider_updated
                        provider_run.excluded_count = excluded_count
                        provider_run.duration_ms = round((monotonic() - started) * 1000)
                        provider_run.finished_at = datetime.now(UTC)
                    found_total += found_count
                    new_total += provider_new
                    updated_total += provider_updated
                    excluded_total += excluded_count
                    successful_providers += 1
                    self.session.commit()
                except AuthorNotFoundError:
                    self.session.rollback()
                    provider_run = self.session.get(ProviderScanRun, provider_run_id)
                    if provider_run:
                        provider_run.status = "succeeded"
                        provider_run.duration_ms = round((monotonic() - started) * 1000)
                        provider_run.finished_at = datetime.now(UTC)
                    successful_providers += 1
                    self.session.commit()
                except Exception as exc:
                    self.session.rollback()
                    message = str(exc)
                    errors.append(f"{provider.display_name}: {message}")
                    provider_run = self.session.get(ProviderScanRun, provider_run_id)
                    if provider_run:
                        provider_run.status = "failed"
                        provider_run.error = message[:4000]
                        provider_run.duration_ms = round((monotonic() - started) * 1000)
                        provider_run.finished_at = datetime.now(UTC)
                    self.session.commit()
        finally:
            await cover_hasher.close()

        if successful_providers:
            aggregation.prune_pending_suggestions(author.id)
            author = AuthorRepository(self.session).get(author_id)
            if author:
                author.last_checked_at = datetime.now(UTC)

        run = self.session.get(DiscoveryRun, run_id)
        if run:
            run.found_count = found_total
            run.new_count = new_total
            run.updated_count = updated_total
            run.excluded_count = excluded_total
            run.finished_at = datetime.now(UTC)
            run.error = "; ".join(errors)[:4000] or None
            run.status = (
                "failed"
                if errors and successful_providers == 0
                else "partial"
                if errors
                else "succeeded"
            )
        self.session.commit()

        if errors and successful_providers == 0:
            raise RuntimeError("; ".join(errors))
        if errors:
            logger.warning("部分漫画来源查询失败: %s", "; ".join(errors))

        return {
            "run_id": run_id,
            "found": found_total,
            "new": new_total,
            "updated": updated_total,
            "excluded": excluded_total,
            "status": run.status if run else "succeeded",
        }
