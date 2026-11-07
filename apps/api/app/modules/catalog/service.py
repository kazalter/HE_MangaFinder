import asyncio
import logging
from datetime import UTC, datetime
from time import monotonic

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import (
    AuthorIdentitySuggestion,
    AuthorWork,
    DiscoveryRun,
    ProviderScanRun,
    Work,
)
from app.modules.authors.repository import AuthorRepository
from app.modules.catalog.aggregation import AggregationService, CoverHasher
from app.modules.catalog.cover_fingerprint import is_current_fingerprint
from app.modules.catalog.discovery_repository import DiscoveryRepository
from app.modules.catalog.repository import CatalogRepository
from app.modules.social.planned_releases import PlannedReleaseService
from app.providers.base import DiscoveredWork, sort_discovered_works
from app.providers.errors import AuthorIdentityUnconfirmedError, AuthorNotFoundError
from app.providers.registry import ProviderRegistry

logger = logging.getLogger(__name__)


class PrefetchedCovers:
    """Only return pre-fetched cover data while the catalog transaction is writing."""

    def __init__(self, values: dict[tuple[str, str | None, str | None], dict | None]):
        self.values = values

    async def fingerprint_url(self, url, provider_name=None, external_id=None):
        return self.values.get((url, provider_name, external_id))


class DiscoveryService:
    def __init__(self, session: Session, providers: ProviderRegistry) -> None:
        self.session = session
        self.providers = providers

    def _record_identity_suggestions(
        self,
        author_id: int,
        provider: str,
        candidates: list[AuthorIdentityUnconfirmedError],
    ) -> None:
        for candidate in candidates:
            name = candidate.candidate[:200]
            exists = self.session.scalar(
                select(AuthorIdentitySuggestion.id).where(
                    AuthorIdentitySuggestion.author_id == author_id,
                    AuthorIdentitySuggestion.provider == provider,
                    AuthorIdentitySuggestion.name == name,
                )
            )
            if not exists:
                self.session.add(
                    AuthorIdentitySuggestion(
                        author_id=author_id,
                        provider=provider,
                        name=name,
                    )
                )

    async def _prefetch_covers(
        self,
        author_id: int,
        provider_name: str,
        works: list[DiscoveredWork],
        hasher: CoverHasher,
    ) -> dict[tuple[str, str | None, str | None], dict | None]:
        # Read all candidates before writing. Cover requests must never run under a
        # SQLite write lock, including requests for existing comparison candidates.
        existing = list(
            self.session.scalars(
                select(Work)
                .join(AuthorWork, AuthorWork.work_id == Work.id)
                .where(AuthorWork.author_id == author_id)
            ).unique()
        )
        requests: set[tuple[str, str | None, str | None]] = set()
        for work in existing:
            current = work.fingerprint.cover_fingerprint if work.fingerprint else None
            if not work.cover_url or (
                is_current_fingerprint(current) and current.get("source_url") == work.cover_url
            ):
                continue
            for source in work.sources:
                requests.add((work.cover_url, source.provider, source.external_id))
        known = {
            source.external_id: work
            for work in existing
            for source in work.sources
            if source.provider == provider_name
        }
        for work in works:
            if not work.cover_url:
                continue
            existing_work = known.get(work.external_id)
            current = (
                existing_work.fingerprint.cover_fingerprint
                if existing_work and existing_work.fingerprint
                else None
            )
            if is_current_fingerprint(current) and current.get("source_url") == work.cover_url:
                continue
            requests.add((work.cover_url, provider_name, work.external_id))
        semaphore = asyncio.Semaphore(3)

        async def fetch(key):
            async with semaphore:
                return key, await hasher.fingerprint_url(*key)

        return dict(await asyncio.gather(*(fetch(key) for key in requests)))

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
        aggregation = AggregationService(self.session)
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
                decision = catalog.author_query_name_decision(author.id, provider.name, author.name)
                # Historical tags are suggestions, not proof that a collaborator is an alias.
                # Explicit provider names are trusted because the user configured them.
                all_names = {author.name, *(author.aliases or []), *(author.circle_names or [])}
                if decision.origin == "manual":
                    all_names.add(decision.query_name)
                inferred_unconfirmed = (
                    decision.origin == "inferred"
                    and decision.query_name.casefold()
                    not in {name.casefold() for name in all_names}
                )
                query_name = author.name if inferred_unconfirmed else decision.query_name
                search_names: list[str] = [query_name]
                for alias in [*(author.aliases or []), *(author.circle_names or [])]:
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
                    name_errors: list[str] = []
                    unconfirmed: list[AuthorIdentityUnconfirmedError] = []
                    for search_name in search_names:
                        try:
                            found = await provider.discover_by_author(
                                search_name,
                                author_names=all_names,
                            )
                            for work in found:
                                if work.external_id not in merged:
                                    merged[work.external_id] = work
                        except AuthorIdentityUnconfirmedError as exc:
                            unconfirmed.append(exc)
                        except AuthorNotFoundError:
                            continue
                        except Exception as exc:
                            name_errors.append(f"{search_name}: {exc}")

                    if not merged and unconfirmed and not name_errors:
                        self._record_identity_suggestions(author.id, provider.name, unconfirmed)
                        provider_run = self.session.get(ProviderScanRun, provider_run_id)
                        if provider_run:
                            provider_run.status = "succeeded"
                            provider_run.error = "; ".join(str(item) for item in unconfirmed)[:4000]
                            provider_run.duration_ms = round((monotonic() - started) * 1000)
                            provider_run.finished_at = datetime.now(UTC)
                        errors.append(
                            f"{provider.display_name}: "
                            + "; ".join(str(item) for item in unconfirmed)
                        )
                        successful_providers += 1
                        self.session.commit()
                        continue
                    if not merged and name_errors:
                        raise RuntimeError("; ".join(name_errors))
                    if not merged:
                        raise AuthorNotFoundError(
                            f"{provider.display_name} 使用所有已知名称均未找到作品"
                        )

                    works = sort_discovered_works(list(merged.values()))
                    excluded_ids = catalog.excluded_external_ids(author.id, provider.name)
                    accepted = [work for work in works if work.external_id not in excluded_ids]
                    prefetched = await self._prefetch_covers(
                        author.id, provider.name, accepted, cover_hasher
                    )
                    aggregation.cover_hasher = PrefetchedCovers(prefetched)
                    self._record_identity_suggestions(author.id, provider.name, unconfirmed)
                    provider_new = 0
                    provider_updated = 0
                    for work in accepted:
                        result = catalog.upsert_with_result(author.id, provider.name, work)
                        group = await aggregation.assign(result.work, author)
                        PlannedReleaseService(self.session).suggest_catalog_match(author.id, group)
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
                        provider_run.status = "failed" if name_errors else "succeeded"
                        if name_errors:
                            provider_run.error = "; ".join(name_errors)[:4000]
                        elif unconfirmed:
                            provider_run.error = "; ".join(str(item) for item in unconfirmed)[:4000]
                        elif inferred_unconfirmed:
                            provider_run.error = (
                                f"来源历史名称“{decision.query_name}”尚未确认；"
                                "请核对后加入作者别名或来源专用名"
                            )
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
                    if name_errors:
                        errors.append(f"{provider.display_name}: {'; '.join(name_errors)}")
                    if unconfirmed:
                        errors.append(
                            f"{provider.display_name}: "
                            + "; ".join(str(item) for item in unconfirmed)
                        )
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
