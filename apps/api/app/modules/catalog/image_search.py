from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db.models import Work, WorkFingerprint, WorkGroupMember
from app.modules.catalog.cover_fingerprint import compare_fingerprints


@dataclass(frozen=True, slots=True)
class ImageSearchMatch:
    group_id: int
    group_title: str
    work_id: int
    edition_title: str
    cover_url: str | None
    distance: int
    mode: str
    confidence: str
    evidence: tuple[str, ...]


class LocalImageSearch:
    """Compares an in-memory query only with persisted local cover fingerprints."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def search(
        self, query_fingerprint: dict[str, object], *, limit: int = 20
    ) -> list[ImageSearchMatch]:
        fingerprints = self.session.scalars(
            select(WorkFingerprint).options(
                selectinload(WorkFingerprint.work)
                .selectinload(Work.group_membership)
                .selectinload(WorkGroupMember.group)
            )
        )
        best_by_group: dict[int, ImageSearchMatch] = {}
        for fingerprint in fingerprints:
            membership = fingerprint.work.group_membership
            if not membership:
                continue
            comparison = compare_fingerprints(
                query_fingerprint,
                fingerprint.cover_fingerprint,
                right_legacy=fingerprint.cover_hash,
            )
            if not comparison or comparison.distance > 20:
                continue
            confidence = (
                "exact"
                if comparison.distance <= 4
                else "strong"
                if comparison.distance <= 8
                else "possible"
            )
            evidence = (
                "本地封面指纹匹配",
                f"裁切感知模式：{comparison.mode}",
                f"视觉距离：{comparison.distance}/64",
                "组合 dHash、pHash 与颜色分布",
            )
            match = ImageSearchMatch(
                group_id=membership.group_id,
                group_title=membership.group.title,
                work_id=fingerprint.work_id,
                edition_title=fingerprint.work.title,
                cover_url=fingerprint.work.cover_url,
                distance=comparison.distance,
                mode=comparison.mode,
                confidence=confidence,
                evidence=evidence,
            )
            current = best_by_group.get(match.group_id)
            if current is None or match.distance < current.distance:
                best_by_group[match.group_id] = match
        return sorted(
            best_by_group.values(), key=lambda item: (item.distance, item.group_title)
        )[:limit]
