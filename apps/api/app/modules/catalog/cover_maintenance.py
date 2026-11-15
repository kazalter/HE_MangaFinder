import asyncio
import time
from typing import Any

from sqlalchemy import exists, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.observability import capture_metrics, record_metric
from app.db.models import MergeSuggestion, Work, WorkFingerprint, WorkGroup
from app.modules.agent_review.candidates import build_candidate_evidence
from app.modules.catalog.aggregation import CoverHasher
from app.modules.catalog.cover_fingerprint import is_current_fingerprint
from app.providers.registry import ProviderRegistry

_LOCK_RETRY_ATTEMPTS = 10


def _is_sqlite_lock_error(exc: OperationalError) -> bool:
    message = str(exc).lower()
    return "locked" in message or "busy" in message


class CoverFingerprintRefreshService:
    """Backfill versioned fingerprints with bounded download concurrency."""

    def __init__(
        self, session: Session, providers: ProviderRegistry, concurrency: int = 3
    ) -> None:
        self.session = session
        self.providers = providers
        self.concurrency = max(1, min(concurrency, 6))

    async def run(self, force: bool = False, batch_size: int = 75) -> dict[str, int]:
        with capture_metrics("cover_maintenance.run", log_summary=True) as metrics:
            metrics.set_attr("force", force)
            metrics.set_attr("batch_size", batch_size)
            return await self._run_impl(force=force, batch_size=batch_size)

    async def _run_impl(self, force: bool = False, batch_size: int = 75) -> dict[str, int]:
        rows = list(
            self.session.scalars(
                select(WorkFingerprint)
                .join(Work, Work.id == WorkFingerprint.work_id)
                .where(Work.cover_url.is_not(None))
                .order_by(WorkFingerprint.work_id)
            )
        )
        record_metric("cover_works_loaded", len(rows))
        all_pending = [item for item in rows if force or self._needs_refresh(item)]
        pending = all_pending if force else all_pending[: max(1, batch_size)]
        work_items: list[tuple[int, str, str | None, str | None]] = [
            (item.work_id, *self._fingerprint_input(item)) for item in pending
        ]
        # Release the read transaction before downloading remote covers.
        self.session.rollback()

        refreshed = 0
        failed = 0
        stale = 0
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
                for (work_id, url, _, _), result in zip(batch, results, strict=True):
                    if isinstance(result, BaseException) or result is None:
                        failed += 1
                        continue
                    payload = dict(result)
                    payload["source_url"] = url
                    updates.append((work_id, url, payload))
                if updates:
                    batch_refreshed, batch_stale = await self._apply_batch_with_retry(updates)
                    refreshed += batch_refreshed
                    stale += batch_stale
        finally:
            await hasher.close()

        changed_suggestions = await self._refresh_suggestion_reasons_with_retry()
        record_metric("cover_works_refreshed", refreshed)
        record_metric("cover_works_failed", failed)
        return {
            "eligible": len(rows),
            "pending": len(all_pending),
            "processed": len(work_items),
            "remaining": max(0, len(all_pending) - len(work_items)) + stale,
            "refreshed": refreshed,
            "failed": failed,
            "suggestions_updated": changed_suggestions,
        }

    async def _apply_batch_with_retry(
        self, updates: list[tuple[int, str, dict[str, Any]]]
    ) -> tuple[int, int]:
        for attempt in range(_LOCK_RETRY_ATTEMPTS):
            started = time.monotonic()
            try:
                batch_refreshed = 0
                batch_stale = 0
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
                        )
                        .execution_options(synchronize_session=False)
                    )
                    if updated.rowcount:
                        batch_refreshed += 1
                    else:
                        batch_stale += 1
                self.session.commit()
                record_metric("db_write_ms", round((time.monotonic() - started) * 1000))
                return batch_refreshed, batch_stale
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
        value = fingerprint.cover_fingerprint
        return not is_current_fingerprint(value) or value.get(
            "source_url"
        ) != fingerprint.work.cover_url

    @staticmethod
    def _fingerprint_input(fingerprint: WorkFingerprint) -> tuple[str, str | None, str | None]:
        source = next(iter(fingerprint.work.sources), None)
        return (
            fingerprint.work.cover_url,
            source.provider if source else None,
            source.external_id if source else None,
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
