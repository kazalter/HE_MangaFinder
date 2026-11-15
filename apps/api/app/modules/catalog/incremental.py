"""Incremental discovery orchestration, identity caching, and resumable pagination."""

from __future__ import annotations

import asyncio
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.observability import record_metric, set_metric_attr
from app.core.time import as_utc
from app.db.models import (
    Author,
    AuthorWork,
    ProviderAuthorIdentity,
    ProviderScanRun,
    ProviderScanState,
    Work,
)
from app.modules.catalog.aggregation import AggregationService, CoverHasher
from app.modules.catalog.cover_fingerprint import is_current_fingerprint
from app.modules.catalog.discovery_repository import DiscoveryRepository
from app.modules.catalog.repository import CatalogRepository
from app.modules.social.planned_releases import PlannedReleaseService
from app.providers.base import DiscoveredWork, compute_list_signature
from app.providers.errors import (
    AuthorIdentityUnconfirmedError,
    AuthorNotFoundError,
    CachedIdentityInvalidError,
)
from app.providers.nhentai import NhentaiProvider
from app.providers.wnacg import WnacgProvider


def normalize_query_key(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def build_author_config_signature(author: Author, provider_cfg_sig: str) -> str:
    return compute_list_signature(
        {
            "name": author.name,
            "aliases": sorted(author.aliases or []),
            "circles": sorted(author.circle_names or []),
            "provider_queries": sorted((author.provider_query_names or {}).items()),
            "provider_cfg": provider_cfg_sig,
        }
    )


@dataclass
class ScanTarget:
    identity_key: str
    resolved_id: str
    resolved_name: str
    primary_query: str
    scan_config_signature: str
    queries: list[str] = field(default_factory=list)
    from_cache: bool = False


@dataclass
class IncrementalProviderOutcome:
    found_count: int = 0
    new_count: int = 0
    updated_count: int = 0
    excluded_count: int = 0
    pages_covered: int = 0
    total_pages_hint: int | None = None
    scan_mode: str = "recent"
    reached_end: bool = False
    budget_truncated: bool = False
    name_errors: list[str] = field(default_factory=list)
    unconfirmed: list[AuthorIdentityUnconfirmedError] = field(default_factory=list)


class PrefetchedCovers:
    """Only return pre-fetched cover data while the catalog transaction is writing."""

    def __init__(self, values: dict[tuple[str, str | None, str | None], dict | None]) -> None:
        self.values = values

    async def fingerprint_url(
        self,
        url: str | None,
        provider_name: str | None = None,
        external_id: str | None = None,
    ) -> dict | None:
        return self.values.get((url, provider_name, external_id))


async def prefetch_covers_for_page(
    session: Session,
    author_id: int,
    provider_name: str,
    works: list[DiscoveredWork],
    hasher: CoverHasher,
    *,
    include_existing_missing: bool = True,
) -> dict[tuple[str, str | None, str | None], dict | None]:
    """Fetch cover fingerprints before any write transaction begins."""
    existing = list(
        session.scalars(
            select(Work)
            .join(AuthorWork, AuthorWork.work_id == Work.id)
            .where(AuthorWork.author_id == author_id)
        ).unique()
    )
    requests: set[tuple[str, str | None, str | None]] = set()
    if include_existing_missing:
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

    if not requests:
        return {}
    semaphore = asyncio.Semaphore(3)

    async def fetch(key: tuple[str, str | None, str | None]) -> tuple[Any, dict | None]:
        async with semaphore:
            return key, await hasher.fingerprint_url(*key)

    return dict(await asyncio.gather(*(fetch(key) for key in requests)))


def supports_incremental_scan(provider: Any) -> bool:
    return isinstance(provider, (NhentaiProvider, WnacgProvider))


def _save_identity_cache(
    session: Session,
    *,
    author_id: int,
    provider_name: str,
    normalized_query: str,
    author_cfg_sig: str,
    rule_version: str,
    resolved_identity: str,
    resolved_name: str,
    ttl_hours: int,
    now: datetime,
) -> None:
    existing = session.scalar(
        select(ProviderAuthorIdentity).where(
            ProviderAuthorIdentity.author_id == author_id,
            ProviderAuthorIdentity.provider == provider_name,
            ProviderAuthorIdentity.normalized_query == normalized_query,
        )
    )
    expires_at = now + timedelta(hours=max(1, ttl_hours))
    if existing is None:
        existing = ProviderAuthorIdentity(
            author_id=author_id,
            provider=provider_name,
            normalized_query=normalized_query,
            author_config_signature=author_cfg_sig,
            rule_version=rule_version,
            resolved_identity=resolved_identity,
            resolved_name=resolved_name,
            confirmed_at=now,
            expires_at=expires_at,
            last_verified_at=now,
        )
        session.add(existing)
    else:
        existing.author_config_signature = author_cfg_sig
        existing.rule_version = rule_version
        existing.resolved_identity = resolved_identity
        existing.resolved_name = resolved_name
        existing.confirmed_at = now
        existing.expires_at = expires_at
        existing.last_verified_at = now


def _invalidate_identity_cache(
    session: Session, *, author_id: int, provider_name: str, resolved_identity: str
) -> None:
    session.execute(
        delete(ProviderAuthorIdentity).where(
            ProviderAuthorIdentity.author_id == author_id,
            ProviderAuthorIdentity.provider == provider_name,
            ProviderAuthorIdentity.resolved_identity == resolved_identity,
        )
    )


async def _resolve_targets(
    session: Session,
    author: Author,
    provider: NhentaiProvider | WnacgProvider,
    search_names: list[str],
    all_names: set[str],
    settings: Settings,
    *,
    force_refresh: bool,
    now: datetime,
) -> tuple[
    list[ScanTarget],
    list[str],
    list[AuthorIdentityUnconfirmedError],
]:
    author_cfg_sig = build_author_config_signature(author, provider.identity_config_signature())
    cached_rows = {
        row.normalized_query: row
        for row in session.scalars(
            select(ProviderAuthorIdentity).where(
                ProviderAuthorIdentity.author_id == author.id,
                ProviderAuthorIdentity.provider == provider.name,
            )
        )
    }

    targets_by_identity: dict[str, ScanTarget] = {}
    name_errors: list[str] = []
    unconfirmed: list[AuthorIdentityUnconfirmedError] = []
    seen_norm_queries: set[str] = set()
    dirty_identity = False

    for query in search_names:
        norm_query = normalize_query_key(query)
        if not norm_query or norm_query in seen_norm_queries:
            continue
        seen_norm_queries.add(norm_query)

        if isinstance(provider, WnacgProvider):
            identity_key = f"q:{norm_query}"
            scan_sig = f"{provider.scan_config_signature(identity_key)}:{author_cfg_sig}"
            cached = cached_rows.get(norm_query)
            already_cached = (
                not force_refresh
                and cached is not None
                and cached.author_config_signature == author_cfg_sig
                and cached.rule_version == provider.identity_rule_version
                and as_utc(cached.expires_at) > now
            )
            if already_cached:
                record_metric("identity_cache_hits")
            if identity_key not in targets_by_identity:
                targets_by_identity[identity_key] = ScanTarget(
                    identity_key=identity_key,
                    resolved_id=query,
                    resolved_name=query,
                    primary_query=query,
                    scan_config_signature=scan_sig,
                    queries=[query],
                    from_cache=already_cached,
                )
            else:
                targets_by_identity[identity_key].queries.append(query)
            continue

        cached = cached_rows.get(norm_query)
        if (
            not force_refresh
            and cached is not None
            and cached.author_config_signature == author_cfg_sig
            and cached.rule_version == provider.identity_rule_version
            and as_utc(cached.expires_at) > now
        ):
            record_metric("identity_cache_hits")
            identity_key = f"tag:{cached.resolved_identity}"
            scan_sig = f"{provider.scan_config_signature(identity_key)}:{author_cfg_sig}"
            if identity_key not in targets_by_identity:
                targets_by_identity[identity_key] = ScanTarget(
                    identity_key=identity_key,
                    resolved_id=cached.resolved_identity,
                    resolved_name=cached.resolved_name,
                    primary_query=query,
                    scan_config_signature=scan_sig,
                    queries=[query],
                    from_cache=True,
                )
            else:
                targets_by_identity[identity_key].queries.append(query)
            continue

        try:
            identity_key, resolved_id, resolved_name = await provider.resolve_author_identity(
                query, author_names=all_names
            )
        except AuthorIdentityUnconfirmedError as exc:
            unconfirmed.append(exc)
            continue
        except AuthorNotFoundError:
            continue
        except Exception as exc:
            name_errors.append(f"{query}: {exc}")
            continue

        _save_identity_cache(
            session,
            author_id=author.id,
            provider_name=provider.name,
            normalized_query=norm_query,
            author_cfg_sig=author_cfg_sig,
            rule_version=provider.identity_rule_version,
            resolved_identity=resolved_id,
            resolved_name=resolved_name,
            ttl_hours=settings.catalog_identity_cache_ttl_hours,
            now=now,
        )
        dirty_identity = True
        scan_sig = f"{provider.scan_config_signature(identity_key)}:{author_cfg_sig}"
        if identity_key not in targets_by_identity:
            targets_by_identity[identity_key] = ScanTarget(
                identity_key=identity_key,
                resolved_id=resolved_id,
                resolved_name=resolved_name,
                primary_query=query,
                scan_config_signature=scan_sig,
                queries=[query],
                from_cache=False,
            )
        else:
            targets_by_identity[identity_key].queries.append(query)

    if dirty_identity:
        session.commit()
    return list(targets_by_identity.values()), name_errors, unconfirmed


def _get_or_init_scan_state(
    session: Session,
    author_id: int,
    provider_name: str,
    target: ScanTarget,
    now: datetime,
) -> ProviderScanState:
    state = session.scalar(
        select(ProviderScanState).where(
            ProviderScanState.author_id == author_id,
            ProviderScanState.provider == provider_name,
            ProviderScanState.identity_key == target.identity_key,
        )
    )
    if state is None:
        state = ProviderScanState(
            author_id=author_id,
            provider=provider_name,
            identity_key=target.identity_key,
            config_signature=target.scan_config_signature,
            deep_cursor_page=1,
            runs_since_deep_scan=0,
            deep_completed_once=False,
            updated_at=now,
        )
        session.add(state)
        session.flush()
    elif state.config_signature != target.scan_config_signature:
        # Invalidate old pagination progress whenever identity/query/sort config changed.
        state.config_signature = target.scan_config_signature
        state.deep_cursor_page = 1
        state.last_deep_scan_at = None
        state.runs_since_deep_scan = 0
        state.deep_completed_once = False
        state.total_pages_hint = None
        state.updated_at = now
        session.flush()
    return state


def _choose_scan_window(
    state: ProviderScanState,
    *,
    coverage_cap: int,
    settings: Settings,
    force_refresh: bool,
    requested_scan_mode: str | None,
    now: datetime,
) -> tuple[str, int, int]:
    recent_pages = min(coverage_cap, max(1, settings.catalog_recent_scan_overlap_pages))
    deep_budget = min(coverage_cap, max(1, settings.catalog_deep_scan_max_pages_per_run))
    deep_interval_runs = max(1, settings.catalog_deep_reconcile_interval_runs)
    deep_interval_delta = timedelta(hours=max(1, settings.catalog_deep_reconcile_interval_hours))

    deep_due = False
    if requested_scan_mode == "deep":
        deep_due = True
    elif requested_scan_mode == "recent":
        deep_due = False
    elif force_refresh or state.deep_cursor_page > 1 or not state.deep_completed_once:
        deep_due = True
    elif (state.runs_since_deep_scan or 0) >= deep_interval_runs:
        deep_due = True
    elif (
        state.last_deep_scan_at is not None
        and as_utc(state.last_deep_scan_at) + deep_interval_delta <= now
    ):
        deep_due = True

    if deep_due:
        start_page = max(1, state.deep_cursor_page - 1) if state.deep_cursor_page > 1 else 1
        start_page = min(start_page, coverage_cap)
        end_budget_page = min(coverage_cap, start_page + deep_budget - 1)
        return "deep", start_page, end_budget_page
    return "recent", 1, recent_pages


async def _fetch_target_page(
    session: Session,
    author: Author,
    provider: NhentaiProvider | WnacgProvider,
    target: ScanTarget,
    all_names: set[str],
    settings: Settings,
    page: int,
    now: datetime,
) -> tuple[list[DiscoveredWork], int, ScanTarget]:
    if isinstance(provider, WnacgProvider):
        works, total_pages = await provider.fetch_search_page(
            target.primary_query, page=page, author_names=all_names
        )
        return works, total_pages, target

    try:
        works, total_pages = await provider.fetch_tagged_page(
            target.resolved_id,
            target.resolved_name,
            page,
            verify_cached_identity=target.from_cache,
        )
        return works, total_pages, target
    except CachedIdentityInvalidError:
        _invalidate_identity_cache(
            session,
            author_id=author.id,
            provider_name=provider.name,
            resolved_identity=target.resolved_id,
        )
        session.commit()
        identity_key, resolved_id, resolved_name = await provider.resolve_author_identity(
            target.primary_query, author_names=all_names
        )
        author_cfg_sig = build_author_config_signature(
            author, provider.identity_config_signature()
        )
        _save_identity_cache(
            session,
            author_id=author.id,
            provider_name=provider.name,
            normalized_query=normalize_query_key(target.primary_query),
            author_cfg_sig=author_cfg_sig,
            rule_version=provider.identity_rule_version,
            resolved_identity=resolved_id,
            resolved_name=resolved_name,
            ttl_hours=settings.catalog_identity_cache_ttl_hours,
            now=now,
        )
        session.commit()
        updated_target = ScanTarget(
            identity_key=identity_key,
            resolved_id=resolved_id,
            resolved_name=resolved_name,
            primary_query=target.primary_query,
            scan_config_signature=f"{provider.scan_config_signature(identity_key)}:{author_cfg_sig}",
            queries=list(target.queries),
            from_cache=False,
        )
        works, total_pages = await provider.fetch_tagged_page(
            updated_target.resolved_id,
            updated_target.resolved_name,
            page,
            verify_cached_identity=False,
        )
        return works, total_pages, updated_target


async def _enrich_page_details(
    provider: NhentaiProvider | WnacgProvider,
    accepted: list[DiscoveredWork],
    fetch_ids: set[str],
) -> list[DiscoveredWork]:
    if not fetch_ids or not isinstance(provider, NhentaiProvider):
        return accepted
    semaphore = asyncio.Semaphore(2)

    async def enrich_one(item: DiscoveredWork) -> DiscoveredWork:
        if item.external_id not in fetch_ids:
            return item
        async with semaphore:
            return await provider.fetch_work_detail(item)

    return list(await asyncio.gather(*(enrich_one(item) for item in accepted)))


async def run_incremental_provider_scan(
    session: Session,
    author: Author,
    provider: NhentaiProvider | WnacgProvider,
    search_names: list[str],
    all_names: set[str],
    *,
    cover_hasher: CoverHasher,
    catalog: CatalogRepository,
    discovery: DiscoveryRepository,
    aggregation: AggregationService,
    settings: Settings,
    force_refresh: bool = False,
    requested_scan_mode: str | None = None,
    now: datetime | None = None,
) -> IncrementalProviderOutcome:
    """Execute identity-cached, detail-cached, resumable discovery for one provider."""
    current_now = as_utc(now) if now is not None else datetime.now(UTC)
    outcome = IncrementalProviderOutcome()
    targets, name_errors, unconfirmed = await _resolve_targets(
        session,
        author,
        provider,
        search_names,
        all_names,
        settings,
        force_refresh=force_refresh,
        now=current_now,
    )
    outcome.name_errors.extend(name_errors)
    outcome.unconfirmed.extend(unconfirmed)

    if not targets:
        if not outcome.name_errors and not outcome.unconfirmed:
            raise AuthorNotFoundError(
                f"{provider.display_name} 使用所有已知名称均未找到作品"
            )
        return outcome

    detail_budget_remaining = max(0, settings.catalog_detail_max_fetches_per_run)
    excluded_ids = catalog.excluded_external_ids(author.id, provider.name)
    seen_external_ids: set[str] = set()
    scan_modes_seen: set[str] = set()
    all_targets_reached_end = True
    any_target_truncated = False
    prefetched_existing_covers = False
    total_matched_works = 0
    planned_releases = PlannedReleaseService(session)

    for target in targets:
        state = _get_or_init_scan_state(
            session, author.id, provider.name, target, current_now
        )

        coverage_cap = max(1, provider.max_search_pages)
        effective_mode, start_page, max_page_this_run = _choose_scan_window(
            state,
            coverage_cap=coverage_cap,
            settings=settings,
            force_refresh=force_refresh,
            requested_scan_mode=requested_scan_mode,
            now=current_now,
        )
        scan_modes_seen.add(effective_mode)

        target_matched = 0
        target_reached_end = False
        target_failed = False
        page = start_page
        first_page_for_target = True

        while page <= max_page_this_run:
            if not first_page_for_target and isinstance(provider, WnacgProvider):
                await provider._sleep(0.25)

            try:
                page_works, total_pages, active_target = await _fetch_target_page(
                    session,
                    author,
                    provider,
                    target,
                    all_names,
                    settings,
                    page,
                    current_now,
                )
            except AuthorIdentityUnconfirmedError as exc:
                outcome.unconfirmed.append(exc)
                target_failed = True
                break
            except AuthorNotFoundError:
                target_reached_end = True
                break
            except Exception as exc:
                outcome.name_errors.append(f"{target.primary_query} p.{page}: {exc}")
                target_failed = True
                break

            if active_target.identity_key != target.identity_key:
                target = active_target
                state = _get_or_init_scan_state(
                    session, author.id, provider.name, target, current_now
                )
                session.commit()
                if page != 1:
                    page = 1
                    continue

            # Handle total_pages shrinking below resumed cursor cleanly.
            if total_pages < start_page and first_page_for_target and page > total_pages:
                page = max(1, total_pages)
                max_page_this_run = page
                first_page_for_target = False
                try:
                    page_works, total_pages, active_target = await _fetch_target_page(
                        session,
                        author,
                        provider,
                        target,
                        all_names,
                        settings,
                        page,
                        current_now,
                    )
                except Exception as exc:
                    outcome.name_errors.append(f"{target.primary_query} p.{page}: {exc}")
                    target_failed = True
                    break

            first_page_for_target = False
            outcome.pages_covered += 1
            outcome.total_pages_hint = (
                total_pages
                if outcome.total_pages_hint is None
                else max(outcome.total_pages_hint, total_pages)
            )
            max_page_this_run = min(max_page_this_run, total_pages, coverage_cap)

            page_unique: list[DiscoveredWork] = []
            page_excluded = 0
            for work in page_works:
                if work.external_id in seen_external_ids:
                    continue
                seen_external_ids.add(work.external_id)
                outcome.found_count += 1
                target_matched += 1
                total_matched_works += 1
                if work.external_id in excluded_ids:
                    page_excluded += 1
                    continue
                page_unique.append(work)

            outcome.excluded_count += page_excluded

            # Select & fetch details outside DB write transactions.
            if isinstance(provider, NhentaiProvider) and page_unique:
                plan = catalog.plan_detail_fetches(
                    provider.name,
                    page_unique,
                    budget=detail_budget_remaining,
                    ttl_hours=settings.catalog_detail_cache_ttl_hours,
                    force=force_refresh,
                    now=current_now,
                )
                if plan.cache_hits:
                    record_metric("detail_cache_hits", plan.cache_hits)
                if plan.fetch_ids:
                    detail_budget_remaining = max(0, detail_budget_remaining - len(plan.fetch_ids))
                    page_unique = await _enrich_page_details(
                        provider, page_unique, plan.fetch_ids
                    )

            # Prefetch cover fingerprints before beginning SQLite writes.
            if page_unique:
                prefetched = await prefetch_covers_for_page(
                    session,
                    author.id,
                    provider.name,
                    page_unique,
                    cover_hasher,
                    include_existing_missing=not prefetched_existing_covers,
                )
                prefetched_existing_covers = True
                aggregation.cover_hasher = PrefetchedCovers(prefetched)

            # Atomic commit of page works, events, and scan cursor progression.
            try:
                for work in page_unique:
                    result = catalog.upsert_with_result(
                        author.id, provider.name, work, now=current_now
                    )
                    group = await aggregation.assign(result.work, author)
                    planned_releases.suggest_catalog_match(author.id, group)
                    if result.source_created or result.author_link_created:
                        discovery.record_event(
                            author_id=author.id,
                            group=group,
                            work_id=result.work.id,
                            provider=provider.name,
                            external_id=work.external_id,
                            title=result.work.title,
                        )
                        outcome.new_count += 1
                        record_metric("new_works")
                    elif result.metadata_changed:
                        outcome.updated_count += 1
                        record_metric("updated_works")
                    else:
                        record_metric("known_works")

                page_state = _get_or_init_scan_state(
                    session, author.id, provider.name, target, current_now
                )
                page_state.total_pages_hint = total_pages
                page_state.last_recent_scan_at = current_now
                page_state.updated_at = current_now

                if page >= total_pages:
                    target_reached_end = True
                    page_state.deep_cursor_page = 1
                    page_state.deep_completed_once = True
                    page_state.last_deep_scan_at = current_now
                    page_state.runs_since_deep_scan = 0
                elif effective_mode == "deep":
                    if page >= coverage_cap:
                        page_state.deep_cursor_page = 1
                        page_state.runs_since_deep_scan = 0
                        any_target_truncated = True
                    elif page == max_page_this_run:
                        page_state.deep_cursor_page = page + 1
                        any_target_truncated = True
                    else:
                        page_state.deep_cursor_page = page + 1
                else:
                    if page == max_page_this_run:
                        page_state.runs_since_deep_scan = (
                            page_state.runs_since_deep_scan or 0
                        ) + 1
                        any_target_truncated = True

                session.commit()
            except Exception as exc:
                session.rollback()
                outcome.name_errors.append(f"{target.primary_query} p.{page} 提交失败: {exc}")
                target_failed = True
                break

            if page >= total_pages:
                break
            page += 1

        if not target_reached_end or target_failed:
            all_targets_reached_end = False
        if (
            isinstance(provider, WnacgProvider)
            and target_matched == 0
            and not target_failed
        ):
            # WNACG search page may return titles whose creator credits don't match.
            continue

        if target_matched > 0 and isinstance(provider, WnacgProvider) and not target.from_cache:
            norm_q = normalize_query_key(target.primary_query)
            author_cfg_sig = build_author_config_signature(
                author, provider.identity_config_signature()
            )
            _save_identity_cache(
                session,
                author_id=author.id,
                provider_name=provider.name,
                normalized_query=norm_q,
                author_cfg_sig=author_cfg_sig,
                rule_version=provider.identity_rule_version,
                resolved_identity=target.resolved_id,
                resolved_name=target.resolved_name,
                ttl_hours=settings.catalog_identity_cache_ttl_hours,
                now=current_now,
            )
            session.commit()

    if outcome.excluded_count:
        record_metric("excluded_works", outcome.excluded_count)

    outcome.scan_mode = (
        next(iter(scan_modes_seen))
        if len(scan_modes_seen) == 1
        else "mixed"
        if scan_modes_seen
        else "recent"
    )
    outcome.reached_end = (
        all_targets_reached_end and not outcome.name_errors and total_matched_works > 0
    )
    outcome.budget_truncated = any_target_truncated

    set_metric_attr("scan_mode", outcome.scan_mode)
    set_metric_attr("reached_end", outcome.reached_end)
    set_metric_attr("budget_truncated", outcome.budget_truncated)

    if total_matched_works == 0 and not outcome.name_errors and not outcome.unconfirmed:
        raise AuthorNotFoundError(
            f"{provider.display_name} 使用所有已知名称均未找到作品"
        )

    return outcome


def populate_provider_scan_run(
    provider_run: ProviderScanRun | None,
    outcome: IncrementalProviderOutcome,
    *,
    started_monotonic: float,
    now: datetime,
    inferred_warning: str | None = None,
) -> None:
    """Record accurate incremental scan metadata on ProviderScanRun."""
    if provider_run is None:
        return
    from time import monotonic

    provider_run.scan_mode = outcome.scan_mode
    provider_run.status = "failed" if outcome.name_errors else "succeeded"
    if outcome.name_errors:
        provider_run.error = "; ".join(outcome.name_errors)[:4000]
    elif outcome.unconfirmed:
        provider_run.error = "; ".join(str(item) for item in outcome.unconfirmed)[:4000]
    elif inferred_warning:
        provider_run.error = inferred_warning
    provider_run.found_count = outcome.found_count
    provider_run.new_count = outcome.new_count
    provider_run.updated_count = outcome.updated_count
    provider_run.excluded_count = outcome.excluded_count
    provider_run.pages_covered = outcome.pages_covered
    provider_run.total_pages_hint = outcome.total_pages_hint
    provider_run.reached_end = outcome.reached_end
    provider_run.budget_truncated = outcome.budget_truncated
    provider_run.duration_ms = round((monotonic() - started_monotonic) * 1000)
    provider_run.finished_at = now
