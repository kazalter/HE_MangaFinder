import asyncio

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import MergeSuggestion, Work, WorkFingerprint, WorkGroup
from app.modules.agent_review.candidates import build_candidate_evidence
from app.modules.catalog.aggregation import CoverHasher
from app.modules.catalog.cover_fingerprint import is_current_fingerprint
from app.providers.registry import ProviderRegistry


class CoverFingerprintRefreshService:
    """Backfill versioned fingerprints with bounded download concurrency."""

    def __init__(
        self, session: Session, providers: ProviderRegistry, concurrency: int = 3
    ) -> None:
        self.session = session
        self.providers = providers
        self.concurrency = max(1, min(concurrency, 6))

    async def run(self, force: bool = False, batch_size: int = 75) -> dict[str, int]:
        rows = list(
            self.session.scalars(
                select(WorkFingerprint)
                .join(Work, Work.id == WorkFingerprint.work_id)
                .where(Work.cover_url.is_not(None))
                .order_by(WorkFingerprint.work_id)
            )
        )
        all_pending = [item for item in rows if force or self._needs_refresh(item)]
        pending = all_pending if force else all_pending[: max(1, batch_size)]
        refreshed = 0
        failed = 0
        hasher = CoverHasher(providers=self.providers)
        try:
            for start in range(0, len(pending), self.concurrency):
                batch = pending[start : start + self.concurrency]
                results = await asyncio.gather(
                    *(self._fingerprint(hasher, item) for item in batch),
                    return_exceptions=True,
                )
                for item, result in zip(batch, results, strict=True):
                    if isinstance(result, BaseException) or result is None:
                        failed += 1
                        continue
                    result["source_url"] = item.work.cover_url
                    item.cover_fingerprint = result
                    variants = result.get("variants", [])
                    item.cover_hash = variants[0].get("dhash") if variants else None
                    refreshed += 1
                self.session.commit()
        finally:
            await hasher.close()
        changed_suggestions = self._refresh_suggestion_reasons()
        self.session.commit()
        return {
            "eligible": len(rows),
            "pending": len(all_pending),
            "processed": len(pending),
            "remaining": max(0, len(all_pending) - len(pending)),
            "refreshed": refreshed,
            "failed": failed,
            "suggestions_updated": changed_suggestions,
        }

    @staticmethod
    def _needs_refresh(fingerprint: WorkFingerprint) -> bool:
        value = fingerprint.cover_fingerprint
        return not is_current_fingerprint(value) or value.get(
            "source_url"
        ) != fingerprint.work.cover_url

    @staticmethod
    async def _fingerprint(
        hasher: CoverHasher, fingerprint: WorkFingerprint
    ) -> dict[str, object] | None:
        source = next(iter(fingerprint.work.sources), None)
        return await hasher.fingerprint_url(
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
