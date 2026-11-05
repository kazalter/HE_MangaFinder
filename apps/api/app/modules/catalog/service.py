import logging
from datetime import UTC, datetime
from time import monotonic

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import DiscoveryRun, ProviderScanRun
from app.modules.authors.repository import AuthorRepository
from app.modules.catalog.aggregation import AggregationService, CoverHasher
from app.modules.catalog.discovery_repository import DiscoveryRepository
from app.modules.catalog.repository import CatalogRepository
from app.modules.social.planned_releases import PlannedReleaseService
from app.providers.base import DiscoveredWork, sort_discovered_works
from app.providers.errors import AuthorNotFoundError
from app.providers.registry import ProviderRegistry

logger = logging.getLogger(__name__)


class DiscoveryService:
    def __init__(self, session: Session, providers: ProviderRegistry) -> None:
        self.session = session
        self.providers = providers

    async def discover_author(
        self,
        author_id: int,
        *,
        trigger: str = "manual",
        provider_names: set[str] | None = None,
    ) -> dict[str, int | str | list[str]]:
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
        skipped_providers: list[str] = []
        providers = [
            provider
            for provider in self.providers.all()
            if provider_names is None or provider.name in provider_names
        ]
        try:
            for provider in providers:
                if trigger in {"scheduled", "provider_retry"}:
                    settings = get_settings()
                    pause_until = discovery.provider_pause_until(
                        provider.name,
                        settings.catalog_provider_retry_base_minutes,
                        settings.catalog_provider_retry_max_minutes,
                    )
                    if pause_until and pause_until > datetime.now(UTC):
                        skipped_providers.append(provider.name)
                        continue
                query_name = catalog.preferred_author_query_name(
                    author.id, provider.name, author.name
                )
                # Build the full set of names for multi-alias search and
                # cross-name verification.
                all_names = {author.name, query_name}
                if author.aliases:
                    all_names.update(author.aliases)
                search_names: list[str] = [query_name]
                for alias in (author.aliases or []):
                    if alias not in search_names:
                        search_names.append(alias)
                if author.name not in search_names:
                    search_names.append(author.name)

                provider_run = discovery.start_provider(run_id, provider.name, query_name)
                self.session.commit()
                provider_run_id = provider_run.id
                started = monotonic()
                try:
                    merged: dict[str, DiscoveredWork] = {}
                    for search_name in search_names:
                        try:
                            found = await provider.discover_by_author(
                                search_name, author_names=all_names,
                            )
                            for work in found:
                                if work.external_id not in merged:
                                    merged[work.external_id] = work
                        except AuthorNotFoundError:
                            continue

                    if not merged:
                        raise AuthorNotFoundError(
                            f"{provider.display_name} 使用所有已知名称均未找到作品"
                        )

                    works = sort_discovered_works(list(merged.values()))
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
                        PlannedReleaseService(self.session).suggest_catalog_match(
                            author.id, group
                        )
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
            if provider_names is None:
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
                "skipped"
                if skipped_providers and not errors and successful_providers == 0
                else "failed"
                if errors and successful_providers == 0
                else "partial"
                if errors
                else "succeeded"
            )
        self.session.commit()

        if errors and successful_providers == 0 and trigger != "provider_retry":
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
            "skipped_providers": skipped_providers,
        }
