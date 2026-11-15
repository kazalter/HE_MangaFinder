import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import exists, func, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.observability import capture_metrics, record_metric
from app.core.time import as_utc
from app.db.models import (
    CoverMaintenanceState,
    MergeSuggestion,
    Work,
    WorkFingerprint,
    WorkGroup,
    WorkSource,
)
from app.modules.agent_review.candidates import build_candidate_evidence
from app.modules.catalog.aggregation import CoverHasher
from app.modules.catalog.cover_fingerprint import is_current_fingerprint
from app.modules.catalog.invalidation import bump_authors_for_works
from app.providers.registry import ProviderRegistry

_LOCK_RETRY_ATTEMPTS = 10
_MAX_COVER_RETRY_ATTEMPTS = 3
_FAILURE_COOLDOWN_HOURS = 24


def _is_sqlite_lock_error(exc: OperationalError) -> bool:
    message = str(exc).lower()
    return "locked" in message or "busy" in message


def _needs_refresh_values(cover_fingerprint: dict[str, Any] | None, cover_url: str) -> bool:
    return (
        not is_current_fingerprint(cover_fingerprint)
        or cover_fingerprint.get("source_url") != cover_url
    )


class CoverFingerprintRefreshService:
    """Backfill versioned fingerprints via bounded keyset pagination and durable checkpoints."""

    def __init__(
        self, session: Session, providers: ProviderRegistry, concurrency: int = 3
    ) -> None:
        self.session = session
        self.providers = providers
        self.concurrency = max(1, min(concurrency, 6))

    async def run(self, force: bool = False, batch_size: int = 75) -> dict[str, Any]:
        with capture_metrics("cover_maintenance.run", log_summary=True) as metrics:
            metrics.set_attr("force", force)
            metrics.set_attr("batch_size", batch_size)
            return await self._run_impl(force=force, batch_size=batch_size)

    async def _run_impl(self, force: bool = False, batch_size: int = 75) -> dict[str, Any]:
        limit = max(1, batch_size)
        mode = "force" if force else "incremental"
        now = datetime.now(UTC)

        round_meta = self._load_round_meta(mode, now)
        if round_meta is None:
            record_metric("cover_works_loaded", 0)
            record_metric("cover_works_refreshed", 0)
            record_metric("cover_works_failed", 0)
            return {
                "eligible": 0,
                "pending": 0,
                "scanned": 0,
                "processed": 0,
                "remaining": 0,
                "refreshed": 0,
                "failed": 0,
                "stale": 0,
                "retry_pending": 0,
                "deferred_failures": 0,
                "has_more": False,
                "next_cursor": None,
                "max_work_id": None,
                "phase": "completed",
                "round_status": "completed",
                "suggestions_updated": 0,
            }

        round_max_id, phase, scan_cursor, existing_retry_queue, round_started_at = round_meta

        scanned = 0
        deferred_failures = 0
        reached_scan_end = phase == "retry"
        selected_urls: list[tuple[int, str]] = []
        remaining_retry_queue: list[int] = []

        if phase == "scan":
            scan_cursor, scanned, deferred_failures, reached_scan_end, selected_urls = (
                self._scan_next_batch(
                    scan_cursor=scan_cursor,
                    round_max_id=round_max_id,
                    limit=limit,
                    force=force,
                    now=now,
                )
            )
            remaining_retry_queue = existing_retry_queue
        else:
            batch_ids = existing_retry_queue[:limit]
            remaining_retry_queue = existing_retry_queue[limit:]
            if batch_ids:
                selected_urls, scanned, deferred_failures = self._load_retry_batch(
                    batch_ids=batch_ids,
                    force=force,
                    now=now,
                )

        work_items = self._attach_sources(selected_urls)
        # Release the read transaction before downloading remote covers.
        self.session.rollback()

        refreshed = 0
        failed = 0
        stale = 0
        new_retry_ids: list[int] = []
        exhausted_failures = 0

        hasher = CoverHasher(providers=self.providers)
        try:
            for start in range(0, len(work_items), self.concurrency):
                batch = work_items[start : start + self.concurrency]
                results = await asyncio.gather(
                    *(
                        hasher.fingerprint_url(url, provider, external_id)
                        for _, url, provider, external_id in batch
                    ),
                    return_exceptions=True,
                )
                updates: list[tuple[int, str, dict[str, Any]]] = []
                failures: list[tuple[int, str]] = []
                for (work_id, url, _, _), result in zip(batch, results, strict=True):
                    if isinstance(result, BaseException) or result is None:
                        failed += 1
                        failures.append((work_id, url))
                        continue
                    payload = dict(result)
                    payload["source_url"] = url
                    updates.append((work_id, url, payload))

                if updates or failures:
                    (
                        batch_refreshed,
                        batch_stale,
                        batch_retry_ids,
                        batch_exhausted,
                    ) = await self._apply_batch_with_retry(updates, failures, force=force)
                    refreshed += batch_refreshed
                    stale += batch_stale
                    new_retry_ids.extend(batch_retry_ids)
                    exhausted_failures += batch_exhausted
        finally:
            await hasher.close()

        next_retry_queue = list(dict.fromkeys([*remaining_retry_queue, *new_retry_ids]))
        if phase == "scan":
            if not reached_scan_end:
                next_phase = "scan"
                has_more = True
            elif next_retry_queue:
                next_phase = "retry"
                has_more = True
            else:
                next_phase = "completed"
                has_more = False
        else:
            if next_retry_queue:
                next_phase = "retry"
                has_more = True
            else:
                next_phase = "completed"
                has_more = False

        await self._persist_state_with_retry(
            mode=mode,
            has_more=has_more,
            next_phase=next_phase,
            scan_cursor=scan_cursor,
            round_max_id=round_max_id,
            retry_queue=next_retry_queue,
            round_started_at=round_started_at,
        )

        changed_suggestions = (
            await self._refresh_suggestion_reasons_with_retry() if refreshed > 0 else 0
        )

        total_deferred = deferred_failures + exhausted_failures
        if has_more:
            round_status = "in_progress"
        elif total_deferred > 0:
            round_status = "completed_with_failures"
        else:
            round_status = "completed"

        if not has_more:
            remaining: int | None = 0
        elif reached_scan_end:
            remaining = len(next_retry_queue)
        else:
            remaining = None

        record_metric("cover_works_loaded", scanned)
        record_metric("cover_works_refreshed", refreshed)
        record_metric("cover_works_failed", failed)
        return {
            "eligible": None if has_more else scanned,
            "pending": None if has_more else 0,
            "scanned": scanned,
            "processed": len(work_items),
            "remaining": remaining,
            "refreshed": refreshed,
            "failed": failed,
            "stale": stale,
            "retry_pending": len(next_retry_queue),
            "deferred_failures": total_deferred,
            "has_more": has_more,
            "next_cursor": scan_cursor if (has_more and next_phase == "scan") else None,
            "max_work_id": round_max_id,
            "phase": next_phase,
            "round_status": round_status,
            "suggestions_updated": changed_suggestions,
        }

    def _load_round_meta(
        self, mode: str, now: datetime
    ) -> tuple[int, str, int, list[int], datetime] | None:
        """Read durable round state without opening a write transaction before HTTP I/O."""
        state = self.session.get(CoverMaintenanceState, mode)
        if state is not None and state.max_work_id is not None:
            return (
                int(state.max_work_id),
                state.phase or "scan",
                int(state.cursor_work_id or 0),
                [int(wid) for wid in (state.retry_work_ids or [])],
                state.round_started_at or now,
            )
        max_id = self.session.scalar(
            select(func.max(WorkFingerprint.work_id))
            .join(Work, Work.id == WorkFingerprint.work_id)
            .where(Work.cover_url.is_not(None), Work.cover_url != "")
        )
        if max_id is None:
            self.session.rollback()
            return None
        return (int(max_id), "scan", 0, [], now)

    def _scan_next_batch(
        self,
        *,
        scan_cursor: int,
        round_max_id: int,
        limit: int,
        force: bool,
        now: datetime,
    ) -> tuple[int, int, int, bool, list[tuple[int, str]]]:
        selected: list[tuple[int, str]] = []
        scanned = 0
        deferred_failures = 0
        reached_end = False

        while len(selected) < limit:
            rows = list(
                self.session.execute(
                    select(
                        WorkFingerprint.work_id,
                        Work.cover_url,
                        WorkFingerprint.cover_fingerprint,
                        WorkFingerprint.cover_next_retry_at,
                        WorkFingerprint.cover_failed_url,
                    )
                    .join(Work, Work.id == WorkFingerprint.work_id)
                    .where(
                        Work.cover_url.is_not(None),
                        Work.cover_url != "",
                        WorkFingerprint.work_id > scan_cursor,
                        WorkFingerprint.work_id <= round_max_id,
                    )
                    .order_by(WorkFingerprint.work_id)
                    .limit(limit)
                )
            )
            if not rows:
                reached_end = True
                break

            for row in rows:
                scanned += 1
                work_id = int(row.work_id)
                cover_url = str(row.cover_url)
                scan_cursor = work_id
                if not force:
                    if not _needs_refresh_values(row.cover_fingerprint, cover_url):
                        continue
                    next_retry_at = row.cover_next_retry_at
                    if (
                        next_retry_at is not None
                        and as_utc(next_retry_at) > now
                        and row.cover_failed_url == cover_url
                    ):
                        deferred_failures += 1
                        continue
                selected.append((work_id, cover_url))
                if len(selected) >= limit:
                    break

            if scan_cursor >= round_max_id:
                reached_end = True
                break
            if len(selected) >= limit:
                has_further = self.session.scalar(
                    select(WorkFingerprint.work_id)
                    .join(Work, Work.id == WorkFingerprint.work_id)
                    .where(
                        Work.cover_url.is_not(None),
                        Work.cover_url != "",
                        WorkFingerprint.work_id > scan_cursor,
                        WorkFingerprint.work_id <= round_max_id,
                    )
                    .limit(1)
                )
                reached_end = has_further is None
                break
            if len(rows) < limit:
                reached_end = True
                break

        return scan_cursor, scanned, deferred_failures, reached_end, selected

    def _load_retry_batch(
        self,
        *,
        batch_ids: list[int],
        force: bool,
        now: datetime,
    ) -> tuple[list[tuple[int, str]], int, int]:
        rows = list(
            self.session.execute(
                select(
                    WorkFingerprint.work_id,
                    Work.cover_url,
                    WorkFingerprint.cover_fingerprint,
                    WorkFingerprint.cover_next_retry_at,
                    WorkFingerprint.cover_failed_url,
                )
                .join(Work, Work.id == WorkFingerprint.work_id)
                .where(
                    WorkFingerprint.work_id.in_(batch_ids),
                    Work.cover_url.is_not(None),
                    Work.cover_url != "",
                )
            )
        )
        by_id = {int(row.work_id): row for row in rows}
        selected: list[tuple[int, str]] = []
        deferred = 0
        for work_id in batch_ids:
            row = by_id.get(work_id)
            if row is None:
                continue
            cover_url = str(row.cover_url)
            if not force:
                if not _needs_refresh_values(row.cover_fingerprint, cover_url):
                    continue
                next_retry_at = row.cover_next_retry_at
                if (
                    next_retry_at is not None
                    and as_utc(next_retry_at) > now
                    and row.cover_failed_url == cover_url
                ):
                    deferred += 1
                    continue
            selected.append((work_id, cover_url))
        return selected, len(rows), deferred

    def _attach_sources(
        self, selected_urls: list[tuple[int, str]]
    ) -> list[tuple[int, str, str | None, str | None]]:
        if not selected_urls:
            return []
        work_ids = [work_id for work_id, _ in selected_urls]
        source_map: dict[int, tuple[str, str]] = {}
        for work_id, provider, external_id in self.session.execute(
            select(WorkSource.work_id, WorkSource.provider, WorkSource.external_id)
            .where(WorkSource.work_id.in_(work_ids))
            .order_by(WorkSource.work_id, WorkSource.id)
        ):
            wid = int(work_id)
            if wid not in source_map:
                source_map[wid] = (str(provider), str(external_id))
        return [
            (work_id, url, *source_map.get(work_id, (None, None)))
            for work_id, url in selected_urls
        ]

    async def _apply_batch_with_retry(
        self,
        updates: list[tuple[int, str, dict[str, Any]]],
        failures: list[tuple[int, str]],
        *,
        force: bool,
    ) -> tuple[int, int, list[int], int]:
        for attempt in range(_LOCK_RETRY_ATTEMPTS):
            started = time.monotonic()
            try:
                now = datetime.now(UTC)
                batch_refreshed = 0
                batch_stale = 0
                refreshed_work_ids: list[int] = []
                retry_ids: list[int] = []
                exhausted = 0

                for work_id, url, payload in updates:
                    variants = payload.get("variants", [])
                    updated = self.session.execute(
                        update(WorkFingerprint)
                        .where(
                            WorkFingerprint.work_id == work_id,
                            exists(
                                select(Work.id).where(
                                    Work.id == work_id, Work.cover_url == url
                                )
                            ),
                        )
                        .values(
                            cover_fingerprint=payload,
                            cover_hash=variants[0].get("dhash") if variants else None,
                            cover_failure_count=0,
                            cover_next_retry_at=None,
                            cover_failed_url=None,
                        )
                        .execution_options(synchronize_session=False)
                    )
                    if updated.rowcount:
                        batch_refreshed += 1
                        refreshed_work_ids.append(work_id)
                    else:
                        batch_stale += 1
                        current_url = self.session.scalar(
                            select(Work.cover_url).where(Work.id == work_id)
                        )
                        if current_url:
                            retry_ids.append(work_id)

                if failures:
                    failed_rows = {
                        row.work_id: row
                        for row in self.session.scalars(
                            select(WorkFingerprint).where(
                                WorkFingerprint.work_id.in_([wid for wid, _ in failures])
                            )
                        )
                    }
                    for work_id, url in failures:
                        fp = failed_rows.get(work_id)
                        if fp is None:
                            continue
                        previous_count = (
                            fp.cover_failure_count if fp.cover_failed_url == url else 0
                        )
                        new_count = previous_count + 1
                        fp.cover_failure_count = new_count
                        fp.cover_failed_url = url
                        # In force mode, cap per-round retries cleanly when already cooled down.
                        round_cap = (
                            max(_MAX_COVER_RETRY_ATTEMPTS, new_count)
                            if (force and previous_count >= _MAX_COVER_RETRY_ATTEMPTS)
                            else _MAX_COVER_RETRY_ATTEMPTS
                        )
                        if new_count < round_cap:
                            fp.cover_next_retry_at = None
                            retry_ids.append(work_id)
                        else:
                            fp.cover_next_retry_at = now + timedelta(hours=_FAILURE_COOLDOWN_HOURS)
                            exhausted += 1

                if refreshed_work_ids:
                    bump_authors_for_works(
                        self.session,
                        refreshed_work_ids,
                        reason="cover_fingerprint_updated",
                        full_reconcile=True,
                    )
                self.session.commit()
                record_metric("db_write_ms", round((time.monotonic() - started) * 1000))
                return batch_refreshed, batch_stale, retry_ids, exhausted
            except OperationalError as exc:
                if not _is_sqlite_lock_error(exc) or attempt >= _LOCK_RETRY_ATTEMPTS - 1:
                    raise
                record_metric("db_lock_retries")
                self.session.rollback()
                await asyncio.sleep(min(1.0, 0.1 * (attempt + 1)))
        raise RuntimeError("unreachable")

    async def _persist_state_with_retry(
        self,
        *,
        mode: str,
        has_more: bool,
        next_phase: str,
        scan_cursor: int,
        round_max_id: int,
        retry_queue: list[int],
        round_started_at: datetime,
    ) -> None:
        for attempt in range(_LOCK_RETRY_ATTEMPTS):
            try:
                state = self.session.get(CoverMaintenanceState, mode)
                if state is None:
                    state = CoverMaintenanceState(mode=mode)
                    self.session.add(state)
                if has_more:
                    state.cursor_work_id = scan_cursor
                    state.max_work_id = round_max_id
                    state.phase = next_phase
                    state.retry_work_ids = retry_queue
                    if state.round_started_at is None:
                        state.round_started_at = round_started_at
                else:
                    state.cursor_work_id = 0
                    state.max_work_id = None
                    state.phase = "scan"
                    state.retry_work_ids = []
                    state.round_started_at = None
                self.session.commit()
                return
            except OperationalError as exc:
                if not _is_sqlite_lock_error(exc) or attempt >= _LOCK_RETRY_ATTEMPTS - 1:
                    raise
                record_metric("db_lock_retries")
                self.session.rollback()
                await asyncio.sleep(min(1.0, 0.1 * (attempt + 1)))
        raise RuntimeError("unreachable")

    async def _refresh_suggestion_reasons_with_retry(self) -> int:
        for attempt in range(_LOCK_RETRY_ATTEMPTS):
            try:
                self.session.expire_all()
                changed_suggestions = self._refresh_suggestion_reasons()
                self.session.commit()
                return changed_suggestions
            except OperationalError as exc:
                if not _is_sqlite_lock_error(exc) or attempt >= _LOCK_RETRY_ATTEMPTS - 1:
                    raise
                self.session.rollback()
                await asyncio.sleep(min(1.0, 0.1 * (attempt + 1)))
        raise RuntimeError("unreachable")

    @staticmethod
    def _needs_refresh(fingerprint: WorkFingerprint) -> bool:
        return _needs_refresh_values(
            fingerprint.cover_fingerprint, str(fingerprint.work.cover_url or "")
        )

    def _refresh_suggestion_reasons(self) -> int:
        changed = 0
        suggestions = list(
            self.session.scalars(
                select(MergeSuggestion).where(MergeSuggestion.status == "pending")
            )
        )
        for suggestion in suggestions:
            left = self.session.get(WorkGroup, suggestion.source_group_id)
            right = self.session.get(WorkGroup, suggestion.target_group_id)
            if not left or not right:
                continue
            evidence = build_candidate_evidence(suggestion, left, right)
            reasons = [
                reason
                for reason in suggestion.reasons
                if not reason.startswith(("封面感知哈希距离", "封面视觉距离"))
            ]
            if evidence.cover_hash_distance is not None:
                label = "封面视觉距离 " + str(evidence.cover_hash_distance)
                if evidence.cover_match_mode == "crop":
                    label += "（同图裁切匹配）"
                reasons.append(label)
            if reasons != suggestion.reasons:
                suggestion.reasons = reasons
                changed += 1
        return changed
